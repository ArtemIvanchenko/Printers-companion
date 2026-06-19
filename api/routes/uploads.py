"""Upload endpoints: log files, new-print form, STL volume estimator."""
from __future__ import annotations

import logging
import os
import shutil
import struct
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from core.config.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["upload"])


# ── Step 2: log file upload ────────────────────────────────────────────────────

_ALLOWED_SUFFIXES = {".log", ".zip"}
_MAX_FILE_MB = 2000


@router.post("/logs")
async def upload_logs(files: list[UploadFile]) -> dict:
    """Save uploaded log files to the raw-logs folder (C:\\PrinterLogs).

    The startup-import task and watcher pick them up automatically.
    Accepts .log and .zip files up to 600 MB each.
    """
    settings = get_settings()
    dest = Path(settings.raw_logs_container_path)
    if not dest.exists():
        raise HTTPException(500, f"Папка логов не найдена: {dest}")

    saved, skipped = [], []
    for f in files:
        name = Path(f.filename or "unknown").name
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            skipped.append({"name": name, "reason": "неподдерживаемый тип файла"})
            continue
        target = dest / name
        total = 0
        too_big = False
        tmp_path = f"/tmp/{os.urandom(8).hex()}.upload"
        try:
            with open(tmp_path, "wb") as buf:
                while chunk := await f.read(16 * 1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_FILE_MB * 1024 * 1024:
                        too_big = True
                        break
                    buf.write(chunk)
            if too_big:
                os.unlink(tmp_path)
                skipped.append({"name": name, "reason": f"файл > {_MAX_FILE_MB} МБ"})
            else:
                shutil.move(tmp_path, target)
                saved.append({"name": name, "size_bytes": total})
                logger.info("upload_logs: saved %s (%d bytes) → %s", name, total, target)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # Trigger re-scan so new files are imported without waiting for next restart
    if saved:
        _trigger_rescan(settings.raw_logs_container_path)

    return {"saved": saved, "skipped": skipped}


@router.post("/rescan")
async def rescan_logs() -> dict:
    """Trigger re-import of all files already in the raw-logs folder.

    Use this when logs were placed directly into the mounted folder
    (bypassing the browser upload), e.g. via network copy or USB.
    """
    settings = get_settings()
    dest = Path(settings.raw_logs_container_path)
    if not dest.exists():
        raise HTTPException(500, f"Папка логов не найдена: {dest}")
    _trigger_rescan(settings.raw_logs_container_path)
    return {"status": "ok", "message": "Сканирование запущено. Новые данные появятся через минуту."}


def _trigger_rescan(path: str) -> None:
    """Ask the API's startup-import logic to re-run in background."""
    import asyncio

    async def _run() -> None:
        try:
            from domain.services.ingestion import IngestionService
            from domain.services.session_grouping import group_files_into_sessions
            from domain.services.session_overview import build_group_overview
            from profiles.m350.profile import build_registry, get_profile
            from storage.db.session import session_scope
            from storage.repositories.runtime import RuntimeRepository

            folder = Path(path)
            result = IngestionService(build_registry(), get_profile()).parse(folder)
            groups = group_files_into_sessions(result.files)
            with session_scope() as db:
                repo = RuntimeRepository(db)
                existing = {sid for sid, _ in repo.list_session_payloads()}
                for group in groups:
                    if group.group_id in existing:
                        continue
                    overview = build_group_overview(
                        group.group_id, group.files,
                        start_ts=group.start_ts, end_ts=group.end_ts,
                        grouping_confidence=group.confidence,
                    )
                    repo.save_session_payload(
                        group.group_id,
                        # Strip parse_result (events): tiny payload; events re-read
                        # from disk on demand (avoids ~96 MB/session in the DB).
                        {"files": [f.model_dump(mode="json", exclude={"parse_result"}) for f in group.files], "group": overview},
                    )
                from domain.services.print_linking import auto_link_print_records

                auto_link_print_records(db)
            logger.info("upload rescan: complete, %d groups", len(groups))
        except Exception:
            logger.exception("upload rescan: failed")

    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_run())
        # Prevent GC from cancelling the task
        _RESCAN_TASKS.add(task)
        task.add_done_callback(_RESCAN_TASKS.discard)
    except RuntimeError:
        pass  # no running loop (tests)


_RESCAN_TASKS: set = set()


# ── Step 3: new-print form ─────────────────────────────────────────────────────

@router.post("/new-print")
async def new_print(payload: dict) -> dict:
    """Record operator data before a print starts.

    Expected body:
      operator   – operator name
      material   – powder material (e.g. AlSi10Mg)
      models     – list of model names / quantities (free text or list)
      note       – optional note
    """
    from storage.db.session import session_scope
    from storage.repositories.runtime import RuntimeRepository
    from domain.models.entities import OperatorEvent

    operator = (payload.get("operator") or "").strip()
    material = (payload.get("material") or "").strip()
    models   = payload.get("models") or []
    note     = (payload.get("note") or "").strip()

    if not operator:
        raise HTTPException(422, "Поле 'operator' обязательно")

    model_list = models if isinstance(models, list) else [models]
    record = {
        "event_id": f"np_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "event_type": "new_print_registered",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "created_by": operator,
        "source_channel": "web",
        "value": material,
        "unit": "material",
        # save_operator_event persists 'note' and 'audit_trail'; the old keys
        # 'free_text'/'meta' were silently dropped (no such columns/handling).
        "note": note,
        "audit_trail": [{"kind": "models", "models": model_list}],
    }

    try:
        with session_scope() as db:
            repo = RuntimeRepository(db)
            repo.save_operator_event(record)
    except Exception:
        logger.exception("new_print: failed to save")
        raise HTTPException(500, "Не удалось сохранить запись")

    logger.info("new_print: %s, material=%s, models=%s", operator, material, models)
    return {"ok": True, "event_id": record["event_id"]}


# ── Step 4: STL volume + print-time estimation ─────────────────────────────────

def _is_binary_stl(data: bytes) -> bool:
    """Reliably detect binary STL vs ASCII STL.

    Many binary STL files (e.g. MagicsX output) have "solid" in the 80-byte
    header, so checking the first 5 bytes alone is not enough.
    Binary STL has an exact size: 84 + 50 * triangle_count bytes.
    """
    if len(data) < 84:
        return True  # too small to be ASCII with any geometry
    count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + 50 * count
    # Allow ±1 byte tolerance for edge cases
    if abs(len(data) - expected) <= 1:
        return True
    # If first 5 bytes are not "solid", definitely binary
    if not data[:5].startswith(b"solid"):
        return True
    return False


def _stl_volume_cm3(data: bytes) -> float:
    """Calculate mesh volume in cm³ using the signed-tetrahedron method.

    Assumes millimetre units (standard for SLM printer STL files).
    """
    if _is_binary_stl(data):
        return _binary_stl_volume(data) / 1000.0  # mm³ → cm³
    try:
        return _ascii_stl_volume(data.decode("utf-8", errors="replace")) / 1000.0
    except Exception:
        return _binary_stl_volume(data) / 1000.0


# M350 build chamber: 350 × 350 × 330 mm → max possible part ~40 000 cm³
_CHAMBER_MAX_CM3 = 40_000
# Files from MagicsX slicer with supports often have "s_" prefix
def _stl_warnings(filename: str, volume_cm3: float) -> list[str]:
    warnings = []
    name = filename.lower()
    if name.startswith("s_") or "_support" in name or "_ex.stl" in name:
        warnings.append(
            "Файл похож на вывод слайсера с подержками (префикс s_ / суффикс _ex). "
            "Для оценки используйте оригинальный STL без поддержек."
        )
    if volume_cm3 > _CHAMBER_MAX_CM3:
        warnings.append(
            f"Объём {volume_cm3:.0f} см³ превышает максимум камеры M350 (~40 000 см³). "
            "Вероятно, файл содержит подержки или несколько деталей."
        )
    if volume_cm3 < 0.01:
        warnings.append("Очень маленький объём — возможно, пустой или повреждённый файл.")
    return warnings


def _binary_stl_volume(data: bytes) -> float:
    if len(data) < 84:
        return 0.0
    count = struct.unpack_from("<I", data, 80)[0]
    vol = 0.0
    offset = 84
    for _ in range(count):
        if offset + 50 > len(data):
            break
        # skip normal (12 bytes), read 3 vertices
        v1 = struct.unpack_from("<3f", data, offset + 12)
        v2 = struct.unpack_from("<3f", data, offset + 24)
        v3 = struct.unpack_from("<3f", data, offset + 36)
        # signed volume of tetrahedron from origin
        vol += (
            v1[0] * (v2[1] * v3[2] - v2[2] * v3[1])
            + v2[0] * (v3[1] * v1[2] - v3[2] * v1[1])
            + v3[0] * (v1[1] * v2[2] - v1[2] * v2[1])
        ) / 6.0
        offset += 50
    return abs(vol)


def _ascii_stl_volume(text: str) -> float:
    import re
    verts = re.findall(r"vertex\s+([\d.e+\-]+)\s+([\d.e+\-]+)\s+([\d.e+\-]+)", text)
    vol = 0.0
    for i in range(0, len(verts) - 2, 3):
        v1 = tuple(float(x) for x in verts[i])
        v2 = tuple(float(x) for x in verts[i + 1])
        v3 = tuple(float(x) for x in verts[i + 2])
        vol += (
            v1[0] * (v2[1] * v3[2] - v2[2] * v3[1])
            + v2[0] * (v3[1] * v1[2] - v3[2] * v1[1])
            + v3[0] * (v1[1] * v2[2] - v1[2] * v2[1])
        ) / 6.0
    return abs(vol)


def _historical_rate() -> dict:
    """Return avg print rate: minutes per cm³, based on completed sessions."""
    try:
        from storage.db.session import SessionLocal
        from storage.repositories.runtime import RuntimeRepository
        with SessionLocal() as db:
            repo = RuntimeRepository(db)
            sessions = repo.list_session_payloads()

        durations, total_sessions = [], 0
        for _, payload in sessions:
            grp = (payload or {}).get("group", {})
            features = grp.get("features", {})
            dur = features.get("duration_min")
            if dur and float(dur) > 10:
                durations.append(float(dur))
                total_sessions += 1

        if not durations:
            return {"sessions_used": 0, "avg_duration_min": None}
        avg = sum(durations) / len(durations)
        return {"sessions_used": total_sessions, "avg_duration_min": round(avg, 1)}
    except Exception:
        return {"sessions_used": 0, "avg_duration_min": None}


def _geometry_prediction(
    data: bytes, material: str, mode: str = "excel", hatch_distance_mm: float | None = None,
    powder_cost_override: float | None = None,
) -> dict:
    """Slice the STL and predict time + cost from machine parameters.

    ``hatch_distance_mm`` — операторский override шага штриховки на этот расчёт
    (шаг зависит от режима печати; задаётся в окошке расчёта). Если не задан,
    берётся значение из параметров машины.
    ``powder_cost_override`` — стоимость порошка из карточки печати; имеет приоритет
    над последней ценой из архива.

    Returns the response "prediction" field; degrades to
    {"available": False, "reason": ...} instead of failing the request.
    """
    from storage.db.session import SessionLocal
    from storage.repositories.prints_repo import PrintsRepository

    try:
        with SessionLocal() as db:
            repo = PrintsRepository(db)
            params = repo.get_machine_params()
            powder_cost = powder_cost_override if powder_cost_override is not None else repo.last_powder_cost()
    except Exception:
        logger.exception("stl_estimate: machine params unavailable")
        return {"available": False, "reason": "База параметров машины недоступна"}

    if hatch_distance_mm and hatch_distance_mm > 0 and params is not None:
        params = {**params, "hatch_distance_mm": float(hatch_distance_mm)}

    from api.routes.machine_settings import params_configured

    if not params_configured(params):
        return {
            "available": False,
            "reason": "Заполните параметры машины (вкладка Архив → Параметры машины)",
        }

    try:
        from analytics.prediction.cost_estimator import estimate_cost
        from analytics.prediction.print_time import EstimationError, estimate_print_time
        from analytics.prediction.stl_slicer import slice_stl

        slices = slice_stl(data, float(params["layer_thickness_mm"]))
        time_est = estimate_print_time(slices, params, material, stl_bytes=data, mode=mode)
        cost_est = estimate_cost(slices, params, material, time_est, powder_cost_override=powder_cost)
    except EstimationError as exc:
        return {"available": False, "reason": str(exc)}
    except Exception:
        logger.exception("stl_estimate: geometry prediction failed")
        return {"available": False, "reason": "Не удалось нарезать модель — проверьте файл"}

    return {
        "available": True,
        "material": material,
        "method": time_est.method,
        "layer_count": slices.layer_count,
        "height_mm": round(slices.height_mm, 2),
        "scan_hours": round(time_est.scan_hours, 2),
        "recoat_hours": round(time_est.recoat_hours, 2),
        "print_hours": round(time_est.print_hours, 2),
        "total_days": round(time_est.total_days, 2),
        "time_breakdown": time_est.breakdown,
        "cost_total_rub": cost_est.total_rub,
        "cost_breakdown": cost_est.breakdown,
        "powder_kg": cost_est.powder_kg,
        "powder_cost_rub_per_kg": powder_cost,
        "warnings": time_est.warnings + cost_est.warnings,
    }


@router.post("/stl-estimate")
async def stl_estimate(
    file: UploadFile,
    material: str = "steel",
    method: str = "fast",
    hatch_distance_mm: float | None = None,
) -> dict:
    """Upload an STL file → volume, historical reference and (when machine
    parameters are configured) a geometry-based time + cost prediction.

    method=fast     → операторская Excel-формула («рассчитать быстро»)
    method=accurate → PySLM, реальные траектории лазера («рассчитать точно»)
    hatch_distance_mm → override шага штриховки на этот расчёт (зависит от режима)
    """
    if not (file.filename or "").lower().endswith(".stl"):
        raise HTTPException(422, "Ожидается файл .stl")
    if method not in ("fast", "accurate"):
        raise HTTPException(422, "method должен быть 'fast' или 'accurate'")
    if hatch_distance_mm is not None and hatch_distance_mm <= 0:
        raise HTTPException(422, "hatch_distance_mm должен быть > 0")

    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Файл > 200 МБ")

    volume_cm3 = _stl_volume_cm3(data)
    hist = _historical_rate()

    # Average past session duration (hours). Fallback 45 min when no history.
    avg_duration_min = hist["avg_duration_min"] or 45.0
    est_hours = round(avg_duration_min / 60, 1)

    warnings = _stl_warnings(file.filename or "", volume_cm3)
    mode = "pyslm" if method == "accurate" else "excel"
    prediction = _geometry_prediction(
        data, (material or "steel").strip().lower(), mode=mode, hatch_distance_mm=hatch_distance_mm,
    )

    return {
        "filename": file.filename,
        "volume_cm3": round(volume_cm3, 2),
        "volume_mm3": round(volume_cm3 * 1000, 0),
        "warnings": warnings,
        "historical": hist,
        "estimate": {
            "note": "На основе средней длительности предыдущих сессий" if hist["sessions_used"] else "Нет исторических данных",
            "avg_session_hours": est_hours,
            "sessions_used": hist["sessions_used"],
        },
        "prediction": prediction,
    }
