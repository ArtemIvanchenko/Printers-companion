"""Upload endpoints: log files, new-print form, STL volume estimator."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import struct
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from api.upload_limits import read_upload_capped
from core.config.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/upload", tags=["upload"])


# ── Step 2: log file upload ────────────────────────────────────────────────────

_ALLOWED_SUFFIXES = {".log", ".zip"}
_MAX_FILE_MB = 2000


@router.post("/logs")
async def upload_logs(files: list[UploadFile]) -> dict:
    """Save uploaded log files to the raw-logs folder (C:\\PrinterLogs).

    Every saved file becomes a durable import job.  Parsing starts only after
    operator confirmation (unless that policy is explicitly disabled).
    """
    settings = get_settings()
    dest = Path(settings.raw_logs_container_path)
    if not dest.exists():
        raise HTTPException(500, f"Папка логов не найдена: {dest}")

    saved, skipped = [], []
    batch_dir: Path | None = None
    for f in files:
        name = Path(f.filename or "unknown").name
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            skipped.append({"name": name, "reason": "неподдерживаемый тип файла"})
            continue
        if batch_dir is None:
            batch_dir = dest / "incoming" / (
                "upload_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            )
            batch_dir.mkdir(parents=True, exist_ok=False)
        target = batch_dir / name
        total = 0
        too_big = False
        digest = hashlib.sha256()
        tmp_path = f"/tmp/{os.urandom(8).hex()}.upload"
        try:
            with open(tmp_path, "wb") as buf:
                while chunk := await f.read(16 * 1024 * 1024):
                    total += len(chunk)
                    if total > _MAX_FILE_MB * 1024 * 1024:
                        too_big = True
                        break
                    digest.update(chunk)
                    buf.write(chunk)
            if too_big:
                os.unlink(tmp_path)
                skipped.append({"name": name, "reason": f"файл > {_MAX_FILE_MB} МБ"})
            else:
                checksum = digest.hexdigest()
                duplicate = False
                if target.exists():
                    from core.utils.files import sha256_file

                    if await asyncio.to_thread(sha256_file, target) == checksum:
                        duplicate = True
                        os.unlink(tmp_path)
                    else:
                        # Never overwrite a different log with the same name.
                        target = batch_dir / f"{Path(name).stem}__{checksum[:12]}{Path(name).suffix}"
                # /tmp is a tmpfs and the destination a bind mount, so this is a
                # cross-device copy of up to 2 GB — off the event loop.
                if not duplicate:
                    await asyncio.to_thread(shutil.move, tmp_path, target)
                saved.append({
                    "name": name,
                    "stored_name": target.name,
                    "size_bytes": total,
                    "checksum": checksum,
                    "duplicate": duplicate,
                })
                logger.info("upload_logs: saved %s (%d bytes) → %s", name, total, target)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    jobs = _enqueue_import_candidates([batch_dir]) if saved and batch_dir is not None else []

    return {"saved": saved, "skipped": skipped, "jobs": jobs}


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
    jobs = _trigger_rescan(settings.raw_logs_container_path, candidates=[dest])
    waiting = sum(job["status"] == "awaiting_operator_confirmation" for job in jobs)
    return {
        "status": "ok",
        "jobs": jobs,
        "message": f"Найдено заданий: {len(jobs)}; ожидают подтверждения: {waiting}.",
    }


def _enqueue_import_candidates(
    paths: list[Path],
    db=None,
    *,
    print_record_id: str | None = None,
) -> list[dict]:
    """Persist import work before returning; no in-process task can be lost."""
    from api.routes.imports import create_detected_import
    from storage.db.session import session_scope
    from storage.repositories.runtime import RuntimeRepository

    def persist(repo: RuntimeRepository) -> list[dict]:
        jobs: list[dict] = []
        for candidate in paths:
            result = create_detected_import(
                str(candidate),
                repo,
                print_record_id=print_record_id,
            )
            jobs.append(result.job.model_dump(mode="json"))
        return jobs

    if db is not None:
        return persist(RuntimeRepository(db))
    with session_scope() as owned_db:
        return persist(RuntimeRepository(owned_db))


def _trigger_rescan(
    path: str,
    candidates: list[Path] | None = None,
    db=None,
    *,
    print_record_id: str | None = None,
) -> list[dict]:
    """Convert filesystem candidates into the same durable import jobs.

    The default is deliberately one folder job. A flat printer export contains
    many complementary logs for several sessions; enumerating children turns
    every file into an isolated pseudo-session and floods the operator with
    confirmation cards.
    """
    folder = Path(path)
    paths = candidates if candidates is not None else [folder]
    return _enqueue_import_candidates(
        paths,
        db=db,
        print_record_id=print_record_id,
    )


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
        logger.exception("historical-rate computation failed; returning empty estimate")
        return {"sessions_used": 0, "avg_duration_min": None}


_PRESET_SCANNING_KEYS = (
    "hatch_speed_mm_s", "contour_speed_mm_s", "hatch_distance_mm",
    "layer_thickness_mm", "jump_speed_mm_s", "jump_delay_ms",
)


def _merge_preset(params: dict, preset: dict) -> dict:
    """Overlay material-specific preset scanning params onto global machine_params."""
    merged = dict(params)
    for key in _PRESET_SCANNING_KEYS:
        if preset.get(key) is not None:
            merged[key] = preset[key]
    return merged


async def _geometry_prediction(
    data: bytes, material: str, hatch_distance_mm: float | None = None,
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
            preset = repo.get_active_preset_for_material(material)
            powder_cost = powder_cost_override if powder_cost_override is not None else repo.last_powder_cost()
    except Exception:
        logger.exception("stl_estimate: machine params unavailable")
        return {"available": False, "reason": "База параметров машины недоступна"}

    # Merge material preset over global params (preset wins for scanning fields)
    if preset and params is not None:
        params = _merge_preset(params, preset)
    elif preset and params is None:
        params = preset

    if hatch_distance_mm and hatch_distance_mm > 0 and params is not None:
        params = {**params, "hatch_distance_mm": float(hatch_distance_mm)}

    from api.routes.machine_settings import effective_params, missing_for_estimation

    missing = missing_for_estimation(params)
    if missing:
        return {
            "available": False,
            "reason": "Не хватает параметров машины: " + ", ".join(missing)
                      + ". Заполните их в Настройки → Параметры машины.",
        }
    params = effective_params(params)

    try:
        from analytics.prediction.cost_estimator import estimate_cost
        from analytics.prediction.print_time import EstimationError, estimate_print_time
        from analytics.prediction.stl_slicer import slice_stl
        import asyncio

        def _compute():
            slices = slice_stl(data, float(params["layer_thickness_mm"]))
            time_est = estimate_print_time(slices, params, material, stl_bytes=data)
            cost_est = estimate_cost(slices, params, material, time_est, powder_cost_override=powder_cost)
            return slices, time_est, cost_est

        slices, time_est, cost_est = await asyncio.to_thread(_compute)
    except EstimationError as exc:
        return {"available": False, "reason": str(exc)}
    except Exception:
        logger.exception("stl_estimate: geometry prediction failed")
        return {"available": False, "reason": "Не удалось нарезать модель — проверьте файл"}

    prediction = time_est.prediction.to_dict() if time_est.prediction else None
    if prediction is not None and prediction["source"] in ("calculated", "calibrated"):
        try:
            from analytics.prediction.accuracy import calibration_interval_hours
            with SessionLocal() as db2:
                interval = calibration_interval_hours(
                    db2, material, slices.layer_thickness_mm,
                    time_est.raw_scan_hours, time_est.raw_recoat_hours,
                )
            if interval is not None:
                prediction["interval"] = list(interval)
        except Exception:
            logger.exception("stl_estimate: calibration interval lookup failed")

    return {
        "available": True,
        "material": material,
        "method": time_est.method,
        "layer_count": slices.layer_count,
        "height_mm": round(slices.height_mm, 2),
        "build_axis": "Z",
        "scan_hours": round(time_est.scan_hours, 2),
        "recoat_hours": round(time_est.recoat_hours, 2),
        "print_hours": round(time_est.print_hours, 2),
        "raw_print_hours": round(time_est.raw_print_hours, 3),
        "raw_scan_hours": round(time_est.raw_scan_hours, 3),
        "raw_recoat_hours": round(time_est.raw_recoat_hours, 3),
        "correction_factor": round(time_est.correction_factor, 3),
        "total_days": round(time_est.total_days, 2),
        "time_breakdown": time_est.breakdown,
        "cost_total_rub": cost_est.total_rub,
        "cost_breakdown": cost_est.breakdown,
        "powder_kg": cost_est.powder_kg,
        "powder_cost_rub_per_kg": powder_cost,
        "prediction": prediction,
        "cost_prediction": cost_est.prediction.to_dict() if cost_est.prediction else None,
        "warnings": time_est.warnings + cost_est.warnings,
    }


@router.post("/stl-estimate")
async def stl_estimate(
    file: UploadFile,
    material: str = "steel",
    hatch_distance_mm: float | None = None,
) -> dict:
    """Upload an STL file → volume, historical reference and (when machine
    parameters are configured) a geometry-based time + cost prediction.

    Time is always the PySLM vector estimate («точно»); there is no fast mode.
    hatch_distance_mm → override шага штриховки на этот расчёт (зависит от режима)
    """
    if not (file.filename or "").lower().endswith(".stl"):
        raise HTTPException(422, "Ожидается файл .stl")
    if hatch_distance_mm is not None and hatch_distance_mm <= 0:
        raise HTTPException(422, "hatch_distance_mm должен быть > 0")

    data = await read_upload_capped(file, 200 * 1024 * 1024)

    volume_cm3 = _stl_volume_cm3(data)
    hist = _historical_rate()

    # Average past session duration (hours) — shown only as a rough historical
    # reference, never as a per-part prediction. No data → no number (the old
    # 45-min fallback produced a misleading constant 0.8 h for every part).
    avg_min = hist["avg_duration_min"]
    avg_session_hours = round(avg_min / 60, 1) if avg_min else None

    warnings = _stl_warnings(file.filename or "", volume_cm3)
    prediction = await _geometry_prediction(
        data, (material or "steel").strip().lower(), hatch_distance_mm=hatch_distance_mm,
    )

    return {
        "filename": file.filename,
        "volume_cm3": round(volume_cm3, 2),
        "volume_mm3": round(volume_cm3 * 1000, 0),
        "warnings": warnings,
        "historical": hist,
        "estimate": {
            "note": "Средняя длительность прошлых сессий (справочно, не прогноз по этой детали)"
                    if hist["sessions_used"] else "Нет исторических данных",
            "avg_session_hours": avg_session_hours,
            "sessions_used": hist["sessions_used"],
        },
        "prediction": prediction,
    }
