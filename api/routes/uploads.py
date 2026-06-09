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
    for f in files:
        name = Path(f.filename or "unknown").name
        suffix = Path(name).suffix.lower()
        if suffix not in _ALLOWED_SUFFIXES:
            skipped.append({"name": name, "reason": "неподдерживаемый тип файла"})
            continue
        target = dest / name
        data = await f.read()
        if len(data) > _MAX_FILE_MB * 1024 * 1024:
            skipped.append({"name": name, "reason": f"файл > {_MAX_FILE_MB} МБ"})
            continue
        target.write_bytes(data)
        logger.info("upload_logs: saved %s (%d bytes) → %s", name, len(data), target)
        saved.append({"name": name, "size_bytes": len(data)})

    # Trigger re-scan so new files are imported without waiting for next restart
    if saved:
        _trigger_rescan(settings.raw_logs_container_path)

    return {"saved": saved, "skipped": skipped}


def _trigger_rescan(path: str) -> None:
    """Ask the API's startup-import logic to re-run in background."""
    import asyncio

    async def _run() -> None:
        try:
            from domain.services.ingestion import IngestionService
            from domain.services.session_grouping import group_files_into_sessions
            from domain.services.session_overview import build_group_overview
            from profiles.m350.profile import build_registry, get_profile
            from storage.db.session import SessionLocal
            from storage.repositories.runtime import RuntimeRepository

            folder = Path(path)
            result = IngestionService(build_registry(), get_profile()).parse(folder)
            groups = group_files_into_sessions(result.files)
            with SessionLocal() as db:
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
                        {"files": [f.model_dump(mode="json") for f in group.files], "group": overview},
                    )
                repo.commit()
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
    from storage.db.session import SessionLocal
    from storage.repositories.runtime import RuntimeRepository
    from domain.models.entities import OperatorEvent

    operator = (payload.get("operator") or "").strip()
    material = (payload.get("material") or "").strip()
    models   = payload.get("models") or []
    note     = (payload.get("note") or "").strip()

    if not operator:
        raise HTTPException(422, "Поле 'operator' обязательно")

    record = {
        "event_id": f"np_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "event_type": "new_print_registered",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "created_by": operator,
        "source_channel": "web",
        "value": material,
        "unit": "material",
        "free_text": note,
        "meta": {"models": models if isinstance(models, list) else [models]},
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

def _stl_volume_cm3(data: bytes) -> float:
    """Calculate mesh volume from binary or ASCII STL in cubic centimetres.

    Uses the signed-tetrahedron method (divergence theorem).
    Assumes units are millimetres (standard for printer STL files).
    """
    # Try binary STL (most common from slicers)
    if not data[:5].startswith(b"solid"):
        return _binary_stl_volume(data) / 1000.0  # mm³ → cm³

    # ASCII STL — parse vertices
    try:
        text = data.decode("utf-8", errors="replace")
        return _ascii_stl_volume(text) / 1000.0
    except Exception:
        return _binary_stl_volume(data) / 1000.0


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


@router.post("/stl-estimate")
async def stl_estimate(file: UploadFile) -> dict:
    """Upload an STL file → get volume (cm³) + estimated print time.

    Time estimate = volume × coefficient_min_per_cm3.
    The coefficient defaults to the historical average from past sessions
    or 45 min/cm³ if no history exists yet.
    """
    if not (file.filename or "").lower().endswith(".stl"):
        raise HTTPException(422, "Ожидается файл .stl")

    data = await file.read()
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "Файл > 200 МБ")

    volume_cm3 = _stl_volume_cm3(data)
    hist = _historical_rate()

    # Use historical rate or fallback 45 min/cm³
    coef = hist["avg_duration_min"] or 45.0
    est_min = volume_cm3 * coef / max(volume_cm3, 1)  # per-session avg, not per cm³

    # Better: if we can't correlate volume→time yet, just show history avg duration
    est_hours = round(coef / 60, 1)  # avg session duration in hours

    return {
        "filename": file.filename,
        "volume_cm3": round(volume_cm3, 2),
        "volume_mm3": round(volume_cm3 * 1000, 0),
        "historical": hist,
        "estimate": {
            "note": "На основе средней длительности предыдущих сессий" if hist["sessions_used"] else "Нет исторических данных",
            "avg_session_hours": est_hours,
            "sessions_used": hist["sessions_used"],
        },
    }
