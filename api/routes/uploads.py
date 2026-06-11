"""Upload endpoints: log files, new-print form, STL volume estimator."""
from __future__ import annotations

import logging
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
_MAX_FILE_MB = 600


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
    max_bytes = _MAX_FILE_MB * 1024 * 1024
    for f in files:
        # Path(...).name strips any directory components in the client-supplied
        # filename, so a crafted name can't escape the raw-logs folder.
        name = Path(f.filename or "unknown").name
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            skipped.append({"name": name, "reason": "неподдерживаемый тип файла"})
            continue
        target = dest / name
        # Stream to disk in 1 MB chunks instead of f.read() (which would pull the
        # whole 600 MB file into the 1 GB-capped container's RAM and can OOM it).
        # Enforce the size limit mid-stream and clean up a partial/oversized file.
        total = 0
        too_big = False
        try:
            with target.open("wb") as out:
                while chunk := await f.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        too_big = True
                        break
                    out.write(chunk)
        except OSError as exc:
            target.unlink(missing_ok=True)
            skipped.append({"name": name, "reason": f"ошибка записи: {exc}"})
            continue
        if too_big:
            target.unlink(missing_ok=True)
            skipped.append({"name": name, "reason": f"файл > {_MAX_FILE_MB} МБ"})
            continue
        logger.info("upload_logs: saved %s (%d bytes) → %s", name, total, target)
        saved.append({"name": name, "size_bytes": total})

    # Trigger re-scan so new files are imported without waiting for next restart
    if saved:
        _trigger_rescan(settings.raw_logs_container_path)

    return {"saved": saved, "skipped": skipped}


def _trigger_rescan(path: str) -> None:
    """Ask the API's startup-import logic to re-run in background."""
    import asyncio

    async def _run() -> None:
        try:
            from domain.services.session_import import import_new_sessions
            from storage.db.session import SessionLocal
            from storage.repositories.runtime import RuntimeRepository

            with SessionLocal() as db:
                # save_session_payload commits each row internally.
                stats = import_new_sessions(Path(path), RuntimeRepository(db))
            logger.info("upload rescan: complete, %d groups (%d new)", stats["found"], stats["imported"])
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
    from storage.db.session import SessionLocal
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
        with SessionLocal() as db:
            repo = RuntimeRepository(db)
            repo.save_operator_event(record)
            repo.commit()
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
    # Clamp to triangles that actually fit in the file so a crafted header
    # (e.g. count=0xFFFFFFFF) can't spin the loop for minutes.
    count = min(count, (len(data) - 84) // 50)
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
    vol = 0.0
    buf: list[tuple[str, str, str]] = []
    for m in re.finditer(r"vertex\s+([\d.e+\-]+)\s+([\d.e+\-]+)\s+([\d.e+\-]+)", text):
        buf.append(m.groups())  # type: ignore[arg-type]
        if len(buf) == 3:
            v1 = tuple(float(x) for x in buf[0])
            v2 = tuple(float(x) for x in buf[1])
            v3 = tuple(float(x) for x in buf[2])
            vol += (
                v1[0] * (v2[1] * v3[2] - v2[2] * v3[1])
                + v2[0] * (v3[1] * v1[2] - v3[2] * v1[1])
                + v3[0] * (v1[1] * v2[2] - v1[2] * v2[1])
            ) / 6.0
            buf = []
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


@router.post("/stl-estimate")
async def stl_estimate(file: UploadFile) -> dict:
    """Upload an STL file → get its volume (cm³) and a rough time reference.

    NOTE: we do not yet store the printed volume of past sessions, so a true
    volume→time rate (min/cm³) cannot be computed. Until that data exists, the
    time reference is simply the historical *average session duration* — it is
    NOT a function of this STL's volume. The response says so explicitly.
    """
    if not (file.filename or "").lower().endswith(".stl"):
        raise HTTPException(422, "Ожидается файл .stl")

    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Файл > 200 МБ")

    import asyncio
    volume_cm3 = await asyncio.get_running_loop().run_in_executor(None, _stl_volume_cm3, data)
    hist = _historical_rate()

    # Average past session duration (hours). Fallback 45 min when no history.
    avg_duration_min = hist["avg_duration_min"] or 45.0
    est_hours = round(avg_duration_min / 60, 1)

    warnings = _stl_warnings(file.filename or "", volume_cm3)

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
    }
