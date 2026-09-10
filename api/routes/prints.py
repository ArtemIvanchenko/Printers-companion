"""Print archive endpoints: print record CRUD, search and file attachments."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import tempfile
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, BinaryIO

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from api.deps.repositories import get_prints_repository
from api.pagination import LimitParam, PaginatedResponse, SkipParam
from api.workstations import workstation_id
from core.config.settings import get_settings
from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from parsers.common.timestamps import date_hint_from_filename
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import (
    PrintRecordConflict,
    PrintSessionLinkConflict,
    PrintsRepository,
)
from storage.sync.local_outbox import LocalNasOutbox, OutboxFullError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prints", tags=["prints"])

_STATUSES = {"draft", "active", "completed"}
_FILE_TYPES = {"stl", "stl_supports", "magics", "photo", "doc"}
_MAX_UPLOAD_MB = 600
_UPLOAD_CHUNK_BYTES = 1024 * 1024
# Materials offered when machine_params has no densities configured yet
_DEFAULT_MATERIALS = ["steel", "aluminum", "titanium", "other"]
# Scanning fields that a material preset overrides in machine_params
_PRESET_SCANNING_KEYS = (
    "hatch_speed_mm_s", "contour_speed_mm_s", "hatch_distance_mm",
    "layer_thickness_mm", "jump_speed_mm_s", "jump_delay_ms",
)


def _require_local_print(record: dict, *, compute_node_id: str | None = None) -> None:
    node_id = compute_node_id or get_settings().compute_node_id
    try:
        require_compute_owner(
            entity_type="print_record",
            entity_id=str(record["record_id"]),
            origin_compute_node_id=str(record["origin_compute_node_id"]),
            requested_compute_node_id=node_id,
        )
    except ComputeAffinityError as exc:
        raise HTTPException(
            403,
            "Расчёт и загрузка исходных файлов разрешены только на "
            f"ПК-владельце карточки. {exc}",
        ) from exc


def _content_disposition(file_name: str) -> str:
    """attachment header for ``file_name``, safe to place in an HTTP header.

    Uploaded names reach here unchanged apart from path stripping, so a quote or
    newline in one would break out of the quoted-string (or the header itself).
    RFC 5987's filename* carries the real, non-ASCII-capable name; the quoted
    fallback is sanitised for clients that ignore it.
    """
    from urllib.parse import quote

    ascii_fallback = "".join(c for c in file_name if c.isprintable() and c not in '"\\;\r\n')
    ascii_fallback = ascii_fallback.encode("ascii", "ignore").decode() or "download"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(file_name)}"


def _bucket_for(file_type: str) -> str:
    settings = get_settings()
    return {
        "stl": settings.minio_bucket_stls,
        "stl_supports": settings.minio_bucket_stls,
        "magics": settings.minio_bucket_magics,
        "photo": settings.minio_bucket_photos,
        "doc": settings.minio_bucket_docs,
    }[file_type]


def _clean_material(raw: str | None) -> str:
    material = (raw or "").strip().lower()
    if not material:
        raise HTTPException(422, "Поле 'material' не может быть пустым")
    if len(material) > 120:
        raise HTTPException(422, "Поле 'material' слишком длинное (макс. 120)")
    return material


def _parse_iso_datetime(raw, field: str) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        raise HTTPException(422, f"Поле '{field}' должно быть датой ISO (ГГГГ-ММ-ДД)")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_powder_cost(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "Поле 'powder_cost_rub_per_kg' должно быть числом")
    if value < 0:
        raise HTTPException(422, "Цена порошка не может быть отрицательной")
    return value


def _parse_layer_thickness(raw) -> float | None:
    """Layer thickness in mm, or None for "use the machine default"."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "Поле 'layer_thickness_mm' должно быть числом")
    # Loose bounds: SLM layers run roughly 0.02–0.1 mm, but the guard only has
    # to reject nonsense (a value in microns, a negative) — the exact process
    # window is the operator's call, not this endpoint's.
    if not (0.0 < value <= 1.0):
        raise HTTPException(422, "Толщина слоя должна быть в мм, в диапазоне 0–1")
    return value


def _parse_hatch_distance(raw) -> float | None:
    """Hatch distance in mm, or None for "use the material preset"."""
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise HTTPException(422, "Поле 'hatch_distance_mm' должно быть числом")
    # This machine's own logs show applied values from 0.10 to 0.90 mm, and the
    # operator briefly typed 3.00 while editing, so the window is genuinely
    # wide. The guard only rejects nonsense (microns, a negative).
    if not (0.0 < value <= 5.0):
        raise HTTPException(422, "Шаг штриховки должен быть в мм, в диапазоне 0–5")
    return value


def _date_from_text(text: str) -> datetime | None:
    """Print date hint from a record/file name like '23.03.2026_кронштейн'."""
    hint = date_hint_from_filename(Path(text))
    return datetime.combine(hint, time(), tzinfo=timezone.utc) if hint else None


@router.post("")
def create_print(
    payload: dict,
    request: Request,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Create a print record.

    Body: {name, material?, layer_thickness_mm?, hatch_distance_mm?, notes?,
    printed_at?, powder_cost_rub_per_kg?}. When printed_at is omitted, a date
    embedded in the name is used if found; the linked log session overwrites it
    later with the real start time.
    """
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(422, "Поле 'name' обязательно")
    material = _clean_material(payload.get("material") or "steel")
    printed_at = _parse_iso_datetime(payload.get("printed_at"), "printed_at") or _date_from_text(name)

    record = repo.create_print_record({
        "origin_compute_node_id": get_settings().compute_node_id,
        "name": name,
        "material": material,
        "layer_thickness_mm": _parse_layer_thickness(payload.get("layer_thickness_mm")),
        "hatch_distance_mm": _parse_hatch_distance(payload.get("hatch_distance_mm")),
        "notes": (payload.get("notes") or "").strip() or None,
        "printed_at": printed_at,
        "powder_cost_rub_per_kg": _parse_powder_cost(payload.get("powder_cost_rub_per_kg")),
        "updated_by": workstation_id(request),
    })
    repo.flush()
    logger.info("prints: created %s (%s)", record["record_id"], name)
    return record


@router.get("")
def list_prints(
    skip: SkipParam = 0,
    limit: LimitParam = 50,
    q: str | None = None,
    material: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_logs: bool | None = None,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Paginated list, newest print date first. Filters: q (name), material, date range."""
    filters = {
        "query": (q or "").strip() or None,
        "material": (material or "").strip().lower() or None,
        "date_from": _parse_iso_datetime(date_from, "date_from"),
        "date_to": _parse_iso_datetime(date_to, "date_to"),
        "has_logs": has_logs,
    }
    records = repo.list_print_records(skip=skip, limit=limit, **filters)
    files_by_record = repo.list_files_for_records([r["record_id"] for r in records])
    for record in records:
        record["files"] = files_by_record.get(record["record_id"], [])
    _attach_plan_vs_fact(repo, records)
    total = repo.count_print_records(**filters)
    return PaginatedResponse(items=records, total=total, skip=skip, limit=limit).to_dict()


def _print_sync_state() -> dict[str, Any]:
    """Small database fingerprint used by the cross-workstation event stream."""
    from domain.models.prints import PrintRecord
    from storage.db.session import session_scope

    with session_scope() as db:
        count, revision_sum, updated_at = db.execute(select(
            func.count(PrintRecord.record_id),
            func.coalesce(func.sum(PrintRecord.revision), 0),
            func.max(PrintRecord.updated_at),
        )).one()
    return {
        "count": int(count or 0),
        "revision_sum": int(revision_sum or 0),
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


_PRINT_SYNC_SUBSCRIBERS: set[asyncio.Queue[dict[str, Any] | None]] = set()
_PRINT_SYNC_TASK: asyncio.Task | None = None


async def _print_sync_monitor() -> None:
    """One NAS poller per local API process, fanned out to every browser tab."""
    global _PRINT_SYNC_TASK
    previous: dict[str, Any] | None = None
    idle_delay = 2.0
    try:
        while _PRINT_SYNC_SUBSCRIBERS:
            try:
                current = await asyncio.to_thread(_print_sync_state)
                if current != previous:
                    previous = current
                    idle_delay = 2.0
                    for queue in tuple(_PRINT_SYNC_SUBSCRIBERS):
                        if queue.full():
                            try:
                                queue.get_nowait()
                            except asyncio.QueueEmpty:
                                pass
                        queue.put_nowait(current)
                else:
                    idle_delay = min(10.0, idle_delay * 1.5)
            except Exception:
                logger.exception("prints: cross-workstation sync monitor failed")
                for queue in tuple(_PRINT_SYNC_SUBSCRIBERS):
                    if not queue.full():
                        queue.put_nowait(None)
                idle_delay = min(30.0, idle_delay * 2)
            await asyncio.sleep(idle_delay)
    finally:
        _PRINT_SYNC_TASK = None


@router.get("/events")
async def print_events(request: Request) -> StreamingResponse:
    """Notify open operator screens when the shared print archive changes.

    PostgreSQL remains the source of truth; this stream only invalidates local
    browser views.  Polling a three-value aggregate also works during the NAS
    migration before a dedicated message broker is exposed centrally.
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1)

    async def stream():
        global _PRINT_SYNC_TASK
        _PRINT_SYNC_SUBSCRIBERS.add(queue)
        if _PRINT_SYNC_TASK is None or _PRINT_SYNC_TASK.done():
            _PRINT_SYNC_TASK = asyncio.create_task(_print_sync_monitor())
        try:
            while not await request.is_disconnected():
                try:
                    current = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if current is None:
                    yield "event: sync-error\ndata: {}\n\n"
                else:
                    yield "event: print-records\ndata: " + json.dumps(current) + "\n\n"
        finally:
            _PRINT_SYNC_SUBSCRIBERS.discard(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _attach_plan_vs_fact(repo: PrintsRepository, records: list[dict]) -> None:
    """Add a ``summary`` to each record: what was predicted, what happened, the gap.

    The list is where the operator compares the two, so both have to arrive in
    one response — the prediction lives in the record's own snapshot while the
    outcome lives on the linked session, and fetching them separately per row
    would be a query per print.

    The actual is machine time (scan + recoat) whenever the printer's own
    time_log covers the session, never the wall-clock span: the estimate models
    machine time only, so comparing it against a span that includes operator
    pauses reports an error the geometry never made. On one real build that gap
    was 18 of 47.6 hours.
    """
    from analytics.prediction.accuracy import _actual_hours, _machine_hours_from_logs
    from domain.models.sessions import BuildSession

    session_ids = [r["session_id"] for r in records if r.get("session_id")]
    sessions: dict[str, BuildSession] = {}
    if session_ids:
        sessions = {
            s.session_id: s for s in repo.db.scalars(
                select(BuildSession).where(BuildSession.session_id.in_(session_ids))
            ).all()
        }

    for record in records:
        snapshot = (record.get("metadata_json") or {}).get("prediction") or {}
        predicted_hours = snapshot.get("print_hours")

        actual_hours = actual_source = idle_hours = layers = None
        session = sessions.get(record.get("session_id") or "")
        if session is not None:
            features = (
                ((session.context or {}).get("runtime_payload", {}) or {}).get("group", {}) or {}
            ).get("features") or {}
            layers = features.get("layers")
            if features.get("idle_min") is not None:
                idle_hours = round(features["idle_min"] / 60, 2)
            machine_hours = _machine_hours_from_logs(
                session.session_id, snapshot.get("layer_count"), repo.db,
            )
            if machine_hours is not None:
                actual_hours, actual_source = round(machine_hours, 2), "machine_log"
            else:
                wall_hours = _actual_hours(session)
                if wall_hours is not None:
                    actual_hours, actual_source = round(wall_hours, 2), "wall_span"

        error_pct = None
        if predicted_hours and actual_hours:
            error_pct = round((predicted_hours - actual_hours) / actual_hours * 100, 1)

        record["summary"] = {
            "predicted_hours": round(predicted_hours, 2) if predicted_hours else None,
            "predicted_cost_rub": snapshot.get("cost_total_rub"),
            "actual_hours": actual_hours,
            "actual_source": actual_source,
            "idle_hours": idle_hours,
            "layers": layers,
            "error_pct": error_pct,
        }


@router.get("/defaults")
def print_defaults(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Prefill values for the new-print form: last powder price + known materials."""
    params = repo.get_machine_params() or {}
    preset_materials = sorted({p["material"] for p in repo.list_presets()})
    density_materials = sorted((params.get("material_densities") or {}).keys())
    materials = preset_materials or density_materials or _DEFAULT_MATERIALS
    return {
        "powder_cost_rub_per_kg": repo.last_powder_cost(),
        "materials": materials,
    }


@router.get("/unlinked-sessions")
def get_unlinked_sessions(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Log sessions belonging to no print card — surfaced as unfinished work.

    Until a session is linked it contributes nothing: no cost, no
    predicted-vs-actual pair, no calibration input.
    """
    from domain.services.print_linking import unlinked_sessions

    sessions = unlinked_sessions(repo.db)
    return {
        "items": sessions,
        "total": len(sessions),
        "n_prints": sum(1 for s in sessions if s["is_print"]),
    }


@router.get("/prediction-accuracy")
def get_prediction_accuracy(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Predicted vs actual report: scan-time correction factors + recoat time,
    both learned per material from history."""
    from analytics.prediction.accuracy import prediction_accuracy
    from analytics.prediction.recoat_calibration import recoat_accuracy
    from analytics.prediction.scan_calibration import scan_calibration_report

    report = prediction_accuracy(repo.db)
    report["recoat"] = recoat_accuracy(repo.db)
    report["scan"] = scan_calibration_report(repo.db)
    return report


@router.post("/recalibrate")
def recalibrate(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Recompute and apply per-material time-correction factors and recoat time
    from history.

    No-op when pinned manually (correction_locked) — one lock for both.
    """
    from analytics.prediction.accuracy import (
        recalibrate_and_apply,
        try_acquire_calibration_lock,
    )
    from analytics.prediction.recoat_calibration import recalibrate_recoat_and_apply
    from analytics.prediction.scan_calibration import recalibrate_scan_and_apply

    if not try_acquire_calibration_lock(repo.db):
        raise HTTPException(
            409,
            "Калибровка уже выполняется на другом операторском ПК; повторите позже",
        )
    result = recalibrate_and_apply(repo.db)
    result["recoat"] = recalibrate_recoat_and_apply(repo.db)
    result["scan"] = recalibrate_scan_and_apply(repo.db)
    repo.flush()
    return result


def _combined_prediction(
    parts: list[tuple[str, bytes | Path]],
    supports: list[tuple[str, bytes | Path]],
    material: str,
    params: dict,
    powder_cost: float | None,
    geometry_cache: Any | None = None,
    db: Any | None = None,
) -> dict:
    """Time + cost estimate over a full print platform (parts + supports STLs).

    All bodies are co-hatched per plate layer by the layer engine (real vectors,
    shared Z axis) — see analytics.prediction.plate_estimator / layer_engine.

    Powder mass for the cost estimate uses part volume only: sheet supports
    have no meaningful mesh volume (flagged in the response warnings).

    ``geometry_cache`` (typically the calling repo) skips re-slicing when an
    identical set of STL bodies at the same hatch_distance_mm was estimated
    before — see plate_estimator._geometry_cache_key.
    """
    from analytics.prediction.cost_estimator import estimate_cost
    from analytics.prediction.plate_estimator import estimate_plate
    from analytics.prediction.stl_slicer import EstimationError, SliceResult

    try:
        est = estimate_plate(parts, supports, params, material, geometry_cache=geometry_cache)

        combined_slices = SliceResult(
            volume_mm3=est.parts_volume_mm3,
            height_mm=est.height_mm,
            layer_count=est.layer_count,
            layer_thickness_mm=float(params["layer_thickness_mm"]),
        )
        cost_est = estimate_cost(combined_slices, params, material,
                                 est.as_print_time_estimate(),
                                 powder_cost_override=powder_cost)
        cost_warnings = list(cost_est.warnings)
        if supports:
            cost_warnings.append(
                "Масса порошка поддержек не входит в стоимость (объём листовых поддержек не определён)."
            )
    except EstimationError as exc:
        return {"available": False, "reason": str(exc)}
    except Exception:
        logger.exception("prints: combined prediction failed")
        return {"available": False, "reason": "Не удалось нарезать модель — проверьте файлы"}

    prediction = est.prediction.to_dict() if est.prediction else None
    # The physics/calibrated path has no interval of its own (print_time.py has
    # no DB access) — attach one from calibration history here, when there is
    # enough of it. Left None (not fabricated) for the fitted/MODEL path and
    # for materials below MIN_PAIRS_FOR_CALIBRATION. A failure here must not
    # sink the whole estimate — it is extra precision info, not the estimate
    # itself.
    if (
        prediction is not None
        and db is not None
        and prediction["source"] in ("calculated", "calibrated")
        and est.layer_overhead_ms is None
    ):
        try:
            from analytics.prediction.accuracy import calibration_interval_hours
            interval = calibration_interval_hours(
                db, material, float(params["layer_thickness_mm"]),
                est.raw_scan_hours, est.raw_recoat_hours,
            )
            if interval is not None:
                prediction["interval"] = list(interval)
                warning = _calibration_mismatch_warning(
                    est.print_hours,
                    interval,
                    material,
                    float(params["layer_thickness_mm"]),
                )
                if warning:
                    prediction.setdefault("warnings", []).append(warning)
        except Exception:
            logger.exception("prints: calibration interval lookup failed")

    return {
        "available": True,
        "n_parts": sum(1 for b in est.bodies if b.kind == "part"),
        "n_support_bodies": sum(1 for b in est.bodies if b.kind == "support"),
        "method": est.method,
        "build_axis": "Z",
        "build_origin_z_mm": round(est.build_origin_z_mm, 3),
        "build_origin_source": est.build_origin_source,
        "layer_count": est.layer_count,
        "height_mm": round(est.height_mm, 2),
        "print_hours": round(est.print_hours, 3),
        "raw_scan_hours": round(est.raw_scan_hours, 3),
        "raw_recoat_hours": round(est.raw_recoat_hours, 3),
        "raw_print_hours": round(est.raw_print_hours, 3),
        "correction_factor": round(est.correction_factor, 3),
        "scan_hours": round(est.scan_hours, 3),
        "recoat_hours": round(est.recoat_hours, 3),
        "cost_total_rub": cost_est.total_rub,
        "scan_source": est.scan_source,
        "recoat_time_ms": round(est.recoat_time_ms, 1),
        "recoat_time_source": est.recoat_time_source,
        "layer_overhead_ms": (
            round(est.layer_overhead_ms, 1) if est.layer_overhead_ms is not None else None
        ),
        "layer_overhead_source": est.layer_overhead_source,
        "layer_overhead_hours": round(est.layer_overhead_hours, 3),
        "layer_overhead_n_prints": est.layer_overhead_n_prints,
        "layer_overhead_n_layers": est.layer_overhead_n_layers,
        "layer_cycle_n_geometries": est.layer_cycle_n_geometries,
        "layer_cycle_model_version": est.layer_cycle_model_version,
        "minimum_layer_cycle_ms": (
            round(est.minimum_layer_cycle_ms, 1)
            if est.minimum_layer_cycle_ms is not None else None
        ),
        "minimum_layer_cycle_status": est.minimum_layer_cycle_status,
        "minimum_cycle_active_layers": est.minimum_cycle_active_layers,
        "minimum_cycle_training_active_layers": est.minimum_cycle_training_active_layers,
        "minimum_cycle_training_active_prints": est.minimum_cycle_training_active_prints,
        "machine_cycle_hours": round(est.machine_cycle_hours, 3),
        "laser_count": int(params.get("laser_count") or 1),
        "geometry_totals": {
            name: round(float(value), 1)
            for name, value in est.geometry_totals.items()
        },
        "geometry_regions": [
            {
                "name": body.name,
                "kind": body.kind,
                "z_min_mm": round(float(body.z_min_mm), 3) if body.z_min_mm is not None else None,
                "z_max_mm": round(float(body.z_max_mm), 3) if body.z_max_mm is not None else None,
                "height_mm": round(float(body.height_mm), 3),
                "scan_share": round(float(body.scan_share), 6),
                "active_z_intervals_mm": [
                    [round(float(low), 3), round(float(high), 3)]
                    for low, high in body.active_z_intervals_mm
                ],
                "xy_bounds_mm": {
                    "x": [round(float(body.x_min_mm), 3), round(float(body.x_max_mm), 3)],
                    "y": [round(float(body.y_min_mm), 3), round(float(body.y_max_mm), 3)],
                } if None not in (
                    body.x_min_mm, body.x_max_mm, body.y_min_mm, body.y_max_mm,
                ) else None,
            }
            for body in est.bodies
        ],
        "prediction": prediction,
        "cost_prediction": cost_est.prediction.to_dict() if cost_est.prediction else None,
        "warnings": est.warnings + cost_warnings,
        # Per-layer geometry series — persisted into the snapshot so scan
        # calibration can later pair it with real burn_ms without re-slicing.
        "scan_geometry": {
            **est.geometry_series.to_snapshot(),
            "layer_thickness_mm": float(params["layer_thickness_mm"]),
            "laser_count": int(params.get("laser_count") or 1),
        } if est.geometry_series is not None else None,
    }


def _calibration_mismatch_warning(
    point_hours: float,
    interval: tuple[float, float],
    material: str,
    layer_thickness_mm: float,
) -> str | None:
    """Warn when history contradicts the uncalibrated physics point.

    An out-of-range correction factor is deliberately not auto-applied, but
    silently returning the raw point would still make a known-bad number look
    authoritative.  Keep the point for auditability and surface the empirical
    interval as the operator-facing safety signal.
    """
    low, high = interval
    if low <= point_hours <= high:
        return None
    mode = f"{material.strip().lower()}@{layer_thickness_mm:.3f}"
    return (
        f"История режима {mode} не подтверждает физическую точку {point_hours:.2f} ч: "
        f"она вне эмпирического интервала {low:.2f}–{high:.2f} ч. "
        "Используйте интервал и проверьте параметры сканирования/полноту геометрии."
    )


def params_for_record(repo: PrintsRepository, record: dict) -> dict:
    """Scanning parameters for one print, most specific source winning.

    machine_params (global) < material preset < the print's own fields.

    Both per-print overrides exist because this shop changes them per job while
    the machine holds one global value:

    * ``layer_thickness_mm`` also selects which fitted scan model applies —
      those are keyed "material@thickness" and do not transfer across
      thicknesses.
    * ``hatch_distance_mm`` rescales the whole estimate, since scan length goes
      as ~1/hatch. The material preset claimed a fixed 0.12 mm while the
      machine's Monitor100 log recorded 0.16 / 0.10 / 0.90 mm applied on steel
      jobs — a 9x span that landed entirely in the prediction error.

    NULL on the record means "not specified": fall through to the preset, then
    to the machine default, so records that predate these fields are unaffected.
    """
    params = dict(repo.get_machine_params() or {})
    preset = repo.get_active_preset_for_material(record["material"])
    if preset:
        params.update({
            k: v for k, v in preset.items()
            if k in _PRESET_SCANNING_KEYS and v is not None
        })
    for field in ("layer_thickness_mm", "hatch_distance_mm"):
        if record.get(field):
            params[field] = record[field]
    origin = (record.get("metadata_json") or {}).get("build_origin_z_mm")
    if isinstance(origin, (int, float)) and not isinstance(origin, bool):
        params["build_origin_z_mm"] = float(origin)
    return params


def _geometry_quality(record: dict) -> dict:
    """Operator/importer assessment of how complete the attached geometry is."""
    value = (record.get("metadata_json") or {}).get("geometry_quality") or {}
    return value if isinstance(value, dict) else {}


def _assert_geometry_usable(record: dict) -> None:
    """Refuse a customer-facing estimate for a plate known to be incomplete."""
    quality = _geometry_quality(record)
    if quality.get("status") != "incomplete":
        return
    note = quality.get("note") or "компоновка содержит не все детали или поддержки"
    raise HTTPException(
        422,
        "Расчёт заблокирован: геометрия карточки помечена как неполная. " + str(note),
    )


def _prepare_prediction_inputs(
    repo: PrintsRepository,
    record_id: str,
    *,
    compute_node_id: str | None = None,
) -> dict[str, Any]:
    """Read a compact immutable estimate input snapshot from the database."""
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    _require_local_print(record, compute_node_id=compute_node_id)
    _assert_geometry_usable(record)

    files = repo.list_print_files(record_id)
    platform_files = [f for f in files if f["file_type"] in ("stl", "stl_supports")]
    if not platform_files:
        raise HTTPException(422, "К карточке не прикреплён STL")

    from api.routes.machine_settings import effective_params, missing_for_estimation

    material = record["material"]
    params = params_for_record(repo, record)
    missing = missing_for_estimation(params)
    if missing:
        raise HTTPException(
            422,
            "Для расчёта не хватает параметров машины: "
            + ", ".join(missing)
            + ". Заполните их в Настройки → Параметры машины.",
        )
    params = effective_params(params)

    # A print card can identify a physical printer through its linked session
    # (or a forward-compatible metadata field before logs are linked).  The
    # current deployment has one machine-parameter row, so None honestly means
    # "the single configured machine", not the operator PC that ran the job.
    printer_id = None
    if record.get("session_id"):
        from domain.models.sessions import BuildSession

        linked_session = repo.db.get(BuildSession, record["session_id"])
        printer_id = linked_session.printer_id if linked_session is not None else None
    if not printer_id:
        candidate = (record.get("metadata_json") or {}).get("printer_id")
        printer_id = str(candidate) if candidate else None

    # Estimation runs after the DB transaction closes and receives only this
    # immutable params copy. Carry physical-machine identity with it so scan
    # and controller-cycle models resolve against NAS-wide machine-scoped keys.
    params = dict(params)
    if printer_id:
        params["printer_id"] = printer_id

    return {
        "record": record,
        "platform_files": platform_files,
        "material": material,
        "params": params,
        "printer_id": printer_id,
        "powder_cost": record.get("powder_cost_rub_per_kg") or repo.last_powder_cost(),
    }


def _geometry_fingerprint(platform_files: list[dict[str, Any]]) -> str:
    """Content identity for leakage-safe model validation across reprints."""
    identities = sorted(
        (
            str(file.get("file_type") or "unknown"),
            str(
                file.get("checksum")
                or f"missing:{file.get('file_name') or ''}:{file.get('size_bytes') or 0}"
            ),
        )
        for file in platform_files
    )
    encoded = json.dumps(
        identities, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return "files-sha256:" + hashlib.sha256(encoded).hexdigest()


def _prediction_input_hash(prepared: dict[str, Any]) -> str:
    payload = {
        "record_id": prepared["record"]["record_id"],
        "record_revision": prepared["record"]["revision"],
        "material": prepared["material"],
        "params": prepared["params"],
        "powder_cost": prepared["powder_cost"],
        "printer_id": prepared.get("printer_id"),
        "files": [
            {
                "type": f["file_type"],
                "checksum": f["checksum"],
                "uri": f["object_uri"],
            }
            for f in prepared["platform_files"]
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _calculate_prediction_snapshot(
    prepared: dict[str, Any],
    *,
    geometry_cache: Any | None = None,
    db: Any | None = None,
    computed_by: str | None = None,
) -> dict:
    """Download models and calculate locally without requiring an open DB tx."""
    from core.versioning.constants import ANALYSIS_VERSION, APP_VERSION

    record = prepared["record"]
    platform_files = prepared["platform_files"]
    material = prepared["material"]
    params = prepared["params"]

    store = ObjectStore()
    with tempfile.TemporaryDirectory(prefix="printer-estimator-") as temporary_dir:
        temporary_root = Path(temporary_dir)
        parts: list[tuple[str, Path]] = []
        supports: list[tuple[str, Path]] = []
        for index, f in enumerate(platform_files):
            bucket, _, object_name = f["object_uri"].removeprefix("s3://").partition("/")
            local_name = f"{index:04d}_{Path(f['file_name']).name}"
            local_path = store.download_file(
                bucket,
                object_name,
                temporary_root / local_name,
                expected_sha256=f.get("checksum") or None,
            )
            if local_path is None:
                raise HTTPException(
                    503,
                    "STL недоступен или повреждён в хранилище: "
                    f"{f['file_name']}",
                )
            if f["file_type"] == "stl_supports":
                supports.append((f["file_name"], local_path))
            else:
                parts.append((f["file_name"], local_path))
        n_supports = len(supports)

        # Keep the temporary files alive until trimesh has loaded every body
        # and the joint plate calculation is complete. Only mesh arrays, not
        # the original multi-hundred-megabyte blobs, remain in memory.
        result = _combined_prediction(
            parts,
            supports,
            material,
            params,
            prepared["powder_cost"],
            geometry_cache=geometry_cache,
            db=db,
        )
    if not result.get("available"):
        raise HTTPException(422, f"Расчёт недоступен: {result.get('reason')}")

    time_prediction = result.get("prediction")
    cost_prediction = result.get("cost_prediction")
    geometry_quality = dict(_geometry_quality(record))
    if not supports and not geometry_quality:
        geometry_quality = {
            "status": "lower_bound",
            "note": "Support-STL не приложены; время и геометрическая привязка могут быть нижней границей.",
        }
    geometry_quality["build_origin_source"] = result.get("build_origin_source")
    geometry_quality["build_origin_z_mm"] = result.get("build_origin_z_mm")
    quality_status = geometry_quality.get("status") or "standard"
    quality_warning = None
    if quality_status == "lower_bound":
        quality_warning = geometry_quality.get("note") or (
            "Оценка является нижней границей: часть печатаемой геометрии отсутствует."
        )
    prediction_warnings = list((time_prediction or {}).get("warnings", []))
    if quality_warning and quality_warning not in prediction_warnings:
        prediction_warnings.append(str(quality_warning))

    from analytics.prediction.layer_engine import scan_model_key

    machine_cycle_hours = float(result.get("machine_cycle_hours") or result["print_hours"])
    mode_key = scan_model_key(material, float(params["layer_thickness_mm"]))
    machine_mode_key = (
        f"{prepared.get('printer_id') or 'configured-machine'}|{mode_key}|"
        f"lasers={int(result.get('laser_count') or params.get('laser_count') or 1)}"
    )

    from analytics.log_insights.geometry import scan_reference
    from core.versioning.provenance import stable_hash

    snapshot: dict = {
        "estimated_at": datetime.now(timezone.utc).isoformat(),
        "input_revision": record["revision"],
        "input_hash": _prediction_input_hash(prepared),
        "geometry_fingerprint": _geometry_fingerprint(platform_files),
        "computed_by": computed_by,
        "app_version": APP_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "n_parts": len(parts),
        "n_supports": n_supports,
        "material": material,
        "printer_id": prepared.get("printer_id"),
        "machine_scope": "printer" if prepared.get("printer_id") else "single_configured_machine",
        "mode_key": mode_key,
        "machine_mode_key": machine_mode_key,
        # The two geometry inputs that scale the whole estimate. Recorded so a
        # stored prediction can be read back and checked against the machine
        # log — without them there is no way to tell what a number was computed
        # at, which is exactly how a preset's 0.12 mm went unnoticed against
        # 0.90 mm on the machine.
        "layer_thickness_mm": params.get("layer_thickness_mm"),
        "hatch_distance_mm": params.get("hatch_distance_mm"),
        "laser_count": result.get("laser_count", params.get("laser_count")),
        "method": result["method"],
        "build_axis": result.get("build_axis", "Z"),
        "build_origin_z_mm": result.get("build_origin_z_mm"),
        "build_origin_source": result.get("build_origin_source"),
        "layer_count": result.get("layer_count"),
        "print_hours": result["print_hours"],
        # Backward-compatible ``print_hours`` remains scan + recoat. The full
        # machine cycle adds the separately calibrated make-layer residual.
        "machine_cycle_hours": machine_cycle_hours,
        "machine_hours": machine_cycle_hours,
        # raw (uncorrected) hours feed the calibration loop, so the learned
        # factor stays absolute and never compounds on itself.
        "raw_print_hours": result.get("raw_print_hours", result["print_hours"]),
        "raw_scan_hours": result.get("raw_scan_hours"),
        "raw_recoat_hours": result.get("raw_recoat_hours"),
        "scan_hours": result.get("scan_hours"),
        "recoat_hours": result.get("recoat_hours"),
        "recoat_time_ms": result.get("recoat_time_ms"),
        "recoat_time_source": result.get("recoat_time_source"),
        "layer_overhead_ms": result.get("layer_overhead_ms"),
        "layer_overhead_source": result.get("layer_overhead_source"),
        "layer_overhead_hours": result.get("layer_overhead_hours", 0.0),
        "layer_overhead_n_prints": result.get("layer_overhead_n_prints", 0),
        "layer_overhead_n_layers": result.get("layer_overhead_n_layers", 0),
        "layer_cycle_n_geometries": result.get("layer_cycle_n_geometries", 0),
        "layer_cycle_model_version": result.get("layer_cycle_model_version"),
        "minimum_layer_cycle_ms": result.get("minimum_layer_cycle_ms"),
        "minimum_layer_cycle_status": result.get("minimum_layer_cycle_status"),
        "minimum_cycle_active_layers": result.get("minimum_cycle_active_layers", 0),
        "minimum_cycle_training_active_layers": result.get(
            "minimum_cycle_training_active_layers", 0,
        ),
        "minimum_cycle_training_active_prints": result.get(
            "minimum_cycle_training_active_prints", 0,
        ),
        "correction_factor": result.get("correction_factor", 1.0),
        "scan_source": result.get("scan_source", "physics"),
        "scan_timing_reference": scan_reference(params, material, float(params["layer_thickness_mm"]),
                         float(result.get("correction_factor") or 1.0)),
        "process_profile_fingerprint": stable_hash({key: params.get(key) for key in (
            "hatch_speed_mm_s", "hatch_speeds_by_mat", "contour_speed_mm_s", "support_speed_mm_s",
            "jump_speed_mm_s", "jump_delay_ms", "hatch_distance_mm", "layer_thickness_mm", "laser_count",
        )}),
        "cost_total_rub": result["cost_total_rub"],
        "scan_geometry": result.get("scan_geometry"),
        "geometry_totals": result.get("geometry_totals") or {},
        "geometry_regions": result.get("geometry_regions") or [],
        "calculation_inputs": {
            "printer_id": prepared.get("printer_id"),
            "machine_scope": "printer" if prepared.get("printer_id") else "single_configured_machine",
            "material": material,
            "layer_thickness_mm": params.get("layer_thickness_mm"),
            "hatch_distance_mm": params.get("hatch_distance_mm"),
            "laser_count": result.get("laser_count", params.get("laser_count")),
            "layer_overhead_ms": result.get("layer_overhead_ms"),
            "minimum_layer_cycle_ms": result.get("minimum_layer_cycle_ms"),
            "geometry_body_count": len(parts) + n_supports,
            "geometry_layer_count": result.get("layer_count"),
            "machine_mode_key": machine_mode_key,
        },
        "time_breakdown": {
            "scan_hours": result.get("scan_hours"),
            "recoat_hours": result.get("recoat_hours"),
            "layer_overhead_hours": result.get("layer_overhead_hours", 0.0),
            "machine_hours": machine_cycle_hours,
            "scan_source": result.get("scan_source", "physics"),
            "recoat_time_ms_per_layer": result.get("recoat_time_ms"),
            "recoat_source": result.get("recoat_time_source"),
            "layer_overhead_source": result.get("layer_overhead_source"),
            "layer_overhead_n_prints": result.get("layer_overhead_n_prints", 0),
            "layer_overhead_n_layers": result.get("layer_overhead_n_layers", 0),
            "layer_cycle_n_geometries": result.get("layer_cycle_n_geometries", 0),
            "minimum_layer_cycle_ms": result.get("minimum_layer_cycle_ms"),
            "minimum_cycle_active_layers": result.get("minimum_cycle_active_layers", 0),
        },
        # Flat, additive fields from the unified prediction contract — kept
        # flat (not nested under a "prediction" key) so they don't collide
        # with this whole snapshot already being metadata_json["prediction"].
        "prediction_source": (time_prediction or {}).get("source"),
        "prediction_interval": (time_prediction or {}).get("interval"),
        "prediction_warnings": prediction_warnings,
        "prediction_explanation": (time_prediction or {}).get("explanation"),
        "cost_prediction_warnings": (cost_prediction or {}).get("warnings", []),
        "estimate_quality": quality_status,
        "geometry_quality": geometry_quality or None,
    }
    return snapshot


def _enrich_prediction_interval(snapshot: dict, db: Any) -> None:
    """Attach the empirical interval in a short post-compute DB transaction."""
    if snapshot.get("prediction_source") not in ("calculated", "calibrated"):
        return
    if snapshot.get("layer_overhead_ms") is not None:
        # calibration_interval_hours is defined for scan+recoat. Attaching it
        # to a point that already includes base/floor controller time would mix
        # two different quantities; wait for a full-cycle interval model.
        return
    try:
        from analytics.prediction.accuracy import calibration_interval_hours

        interval = calibration_interval_hours(
            db,
            str(snapshot["material"]),
            float(snapshot["layer_thickness_mm"]),
            float(snapshot.get("raw_scan_hours") or 0.0),
            float(snapshot.get("raw_recoat_hours") or 0.0),
        )
        if interval is None:
            return
        snapshot["prediction_interval"] = list(interval)
        warning = _calibration_mismatch_warning(
            float(snapshot["print_hours"]),
            interval,
            str(snapshot["material"]),
            float(snapshot["layer_thickness_mm"]),
        )
        if warning:
            warnings = list(snapshot.get("prediction_warnings") or [])
            if warning not in warnings:
                warnings.append(warning)
            snapshot["prediction_warnings"] = warnings
    except Exception:
        logger.exception("prints: calibration interval lookup failed")


def _store_prediction_snapshot(
    repo: PrintsRepository,
    record_id: str,
    snapshot: dict,
    *,
    expected_revision: int | None = None,
    compute_node_id: str | None = None,
) -> None:
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    _require_local_print(record, compute_node_id=compute_node_id)

    meta = dict(record.get("metadata_json") or {})
    meta["prediction"] = snapshot
    try:
        repo.update_print_record(
            record_id,
            {"metadata_json": meta},
            expected_revision=expected_revision,
        )
    except PrintRecordConflict as conflict:
        raise HTTPException(
            409,
            detail={
                "message": "Карточка изменилась во время расчёта; устаревший результат отброшен",
                "current": conflict.current,
            },
        ) from None
    repo.flush()
    logger.info(
        "prints: prediction stored for %s (%d parts + %d supports, %.1fh, ×%.3f)",
        record_id,
        int(snapshot.get("n_parts") or 0),
        int(snapshot.get("n_supports") or 0),
        snapshot["print_hours"],
        snapshot["correction_factor"],
    )


def _compute_prediction_snapshot(repo: PrintsRepository, record_id: str) -> dict:
    """Compatibility path: compute and store using the caller's transaction.

    Production durable workers use the split prepare/calculate/store functions
    so the long local geometry calculation holds no NAS transaction open.
    """
    prepared = _prepare_prediction_inputs(repo, record_id)
    snapshot = _calculate_prediction_snapshot(
        prepared,
        geometry_cache=repo,
        db=repo.db,
        computed_by=get_settings().compute_node_id,
    )
    _store_prediction_snapshot(
        repo,
        record_id,
        snapshot,
        expected_revision=prepared["record"]["revision"],
    )
    return snapshot


def _assert_estimatable(repo: PrintsRepository, record: dict) -> None:
    """Raise if the estimate cannot run, before committing to a long job.

    Only the cheap preconditions — an attached STL and the machine parameters.
    Running these up front means the operator hears "no STL attached" straight
    away instead of watching a background job produce nothing.
    """
    from api.routes.machine_settings import missing_for_estimation

    _require_local_print(record)
    _assert_geometry_usable(record)

    files = repo.list_print_files(record["record_id"])
    if not [f for f in files if f["file_type"] in ("stl", "stl_supports")]:
        raise HTTPException(422, "К карточке не прикреплён STL")

    # Same resolution the real estimate uses, or this precondition reports a
    # parameter as missing that the record itself supplies.
    missing = missing_for_estimation(params_for_record(repo, record))
    if missing:
        raise HTTPException(
            422,
            "Для расчёта не хватает параметров машины: " + ", ".join(missing)
            + ". Заполните их в Настройки → Параметры машины.",
        )


def _enqueue_estimate(
    repo: PrintsRepository,
    record: dict,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Create a local estimate job for this exact geometry/revision.

    Automatic triggers deduplicate the same inputs. A manual "recalculate"
    always gets a fresh request id, even when the card revision is unchanged.
    """
    from storage.repositories.jobs_repo import JobsRepository

    node_id = get_settings().compute_node_id
    _require_local_print(record, compute_node_id=node_id)
    geometry = sorted(
        (f["file_type"], f["checksum"])
        for f in repo.list_print_files(record["record_id"])
        if f["file_type"] in ("stl", "stl_supports")
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {"revision": record["revision"], "geometry": geometry},
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    request_key = os.urandom(12).hex() if force else fingerprint
    return JobsRepository(repo.db).enqueue(
        job_type="print_estimate",
        owner_node_id=node_id,
        entity_type="print_record",
        entity_id=record["record_id"],
        idempotency_key=(
            f"print_estimate:{node_id}:{record['record_id']}:{request_key}"
        ),
        payload={
            "record_id": record["record_id"],
            "record_revision": record["revision"],
            "owner_node_id": node_id,
            "input_fingerprint": fingerprint,
            "manual_rerun": force,
        },
        max_attempts=3,
    )


# PLAN_ACCURACY.md 2.4. One worker: compute_layer_series already parallelises
# internally across 4 threads (layer_engine._SECTION_THREADS) up to this
# container's own CPU limit — a second concurrent estimate would only fight
# the first one for the same cores, not add real throughput. A second request
# just queues behind it in the pool rather than racing it.
_ESTIMATE_POOL: ProcessPoolExecutor | None = None


def _estimate_pool() -> ProcessPoolExecutor:
    global _ESTIMATE_POOL
    if _ESTIMATE_POOL is None:
        _ESTIMATE_POOL = ProcessPoolExecutor(max_workers=1)
    return _ESTIMATE_POOL


def _run_estimate_in_process(record_id: str) -> None:
    """The actual estimate, run in a separate OS process (see _auto_estimate).

    Needs its own DB session — nothing from the api process's session or
    request state crosses this boundary. Module-level so ProcessPoolExecutor
    can pickle a reference to it (a closure or bound method can't be).
    """
    from storage.db.session import session_scope

    with session_scope() as db:
        repo = PrintsRepository(db)
        _compute_prediction_snapshot(repo, record_id)


async def _auto_estimate(record_id: str) -> None:
    """Background prediction after an STL upload or manual re-estimate.

    The heavy part runs in a separate OS process, not a thread in this one:
    compute_layer_series is CPU-bound Python/C, and a thread here would still
    contend for THIS process's own GIL with every other request this instance
    is serving. There is no other operator sharing this process to blame —
    each PC runs its own full stack — so "every other request" means this same
    operator's own next dashboard click while they wait for their own
    estimate. A subprocess sidesteps that; the container's CPU limit still
    applies (raised to 4.0 for exactly this — see docker-compose.yml).

    Tests run this inline in the same process instead (APP_ENV=test): a real
    subprocess would not see this process's monkeypatched ObjectStore — the
    in-memory store tests substitute for MinIO exists only in this process's
    memory, and a spawned/forked child does not share it.
    """
    try:
        if get_settings().app_env == "test":
            _run_estimate_in_process(record_id)
        else:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(_estimate_pool(), _run_estimate_in_process, record_id)
    except HTTPException as exc:
        # Параметры машины не заполнены и т.п. — это не ошибка загрузки файла
        logger.info("prints: auto-estimate for %s skipped: %s", record_id, exc.detail)
    except Exception:
        logger.exception("prints: auto-estimate for %s failed", record_id)


@router.post("/{record_id}/estimate")
def estimate_print_record(
    record_id: str,
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Manual (re)run of the prediction snapshot for a record's STL.

    Runs in the background and returns immediately. Co-hatching a real plate is
    not fast — a three-part build with 32 MB of support meshes takes minutes of
    solid CPU, and the upload path already treats the estimate as a background
    job for exactly that reason. Holding the request open for it means a
    browser or proxy timeout decides whether the result is kept.

    The heavy part also runs in its own OS process (see _auto_estimate), so
    the rest of this dashboard stays responsive while it grinds through a
    heavy plate instead of contending for this process's own GIL.

    The caller polls GET /prints/{id} and watches for metadata_json.prediction
    to appear or its estimated_at to move.
    """
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    # Fail fast on the cheap preconditions so the operator hears about a
    # missing STL or an unfilled parameter now, not after a silent no-op.
    _assert_estimatable(repo, record)

    job = _enqueue_estimate(repo, record, force=True)
    # TestClient historically observes the completed snapshot immediately and
    # has no estimator service. Preserve that contract without weakening the
    # production path, where only the durable worker performs the calculation.
    if get_settings().app_env == "test":
        repo.db.commit()
        background_tasks.add_task(_auto_estimate, record_id)
    return {
        "record_id": record_id,
        "job_id": job["job_id"],
        "status": "started",
        "job_status": job["status"],
        "previous": (record.get("metadata_json") or {}).get("prediction"),
    }


@router.get("/{record_id}")
def get_print(record_id: str, repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Full print record with files and geometry-aware anomaly locations."""
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    record["files"] = repo.list_print_files(record_id)
    from domain.models.jobs import BackgroundJob
    estimate_job = repo.db.scalar(
        select(BackgroundJob).where(
            BackgroundJob.entity_id == record_id,
            BackgroundJob.job_type == 'print_estimate',
            BackgroundJob.status.in_(['pending', 'running', 'postponed']),
        ).order_by((BackgroundJob.status == 'running').desc(), BackgroundJob.created_at.desc()).limit(1)
    )
    record['estimate_job'] = ({'job_id': estimate_job.job_id, 'status': estimate_job.status}
                              if estimate_job else None)
    from storage.repositories.runtime import RuntimeRepository

    record["quality_outcomes"] = RuntimeRepository(repo.db).list_quality_outcomes(
        print_record_id=record_id,
    )
    snapshot = ((record.get("metadata_json") or {}).get("prediction") or {})
    if record.get("session_id") and snapshot.get("scan_geometry"):
        input_revision = snapshot.get("input_revision")
        snapshot_is_current = (
            not isinstance(input_revision, int)
            or record.get("revision") in {input_revision, input_revision + 1}
        )
        if not snapshot_is_current:
            record["geometry_analysis"] = {
                "status": "stale_prediction",
                "reason_ru": (
                    "Карточка или STL изменились после расчёта; привязка аномалий скрыта "
                    "до повторного расчёта."
                ),
                "items": [],
            }
            return record
        from analytics.geometry_context import map_anomalies_to_geometry
        from domain.models.sessions import BuildSession

        session = repo.db.get(BuildSession, record["session_id"])
        group = (
            (((session.context or {}).get("runtime_payload") or {}).get("group") or {})
            if session is not None else {}
        )
        try:
            record["geometry_analysis"] = map_anomalies_to_geometry(
                group.get("health"),
                snapshot.get("scan_geometry"),
                geometry_regions=snapshot.get("geometry_regions"),
                telemetry=group.get("telemetry"),
                geometry_quality=snapshot.get("geometry_quality"),
            )
        except Exception:
            # This is derived display context over two stored snapshots. A bad
            # legacy snapshot must not make the print card itself unreadable.
            logger.exception("prints: geometry anomaly mapping failed for %s", record_id)
            record["geometry_analysis"] = {
                "status": "unavailable",
                "reason_ru": "Не удалось сопоставить старый снимок геометрии с логами",
                "items": [],
            }
    return record


@router.get("/{record_id}/quality-outcomes")
def list_print_quality_outcomes(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> list[dict]:
    if not repo.get_print_record(record_id):
        raise HTTPException(404, "Карточка печати не найдена")
    from storage.repositories.runtime import RuntimeRepository

    return RuntimeRepository(repo.db).list_quality_outcomes(print_record_id=record_id)


@router.post("/{record_id}/quality-outcomes")
def create_print_quality_outcome(
    record_id: str,
    payload: dict,
    request: Request,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Append an operator-confirmed good/defect label to a print card.

    Labels are append-only at this endpoint: a correction is another, newer
    inspection row. This preserves who concluded what and when, and the latest
    final row becomes the ML ground truth.
    """
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")

    from domain.services.quality import create_final_print_outcome
    from storage.repositories.runtime import RuntimeRepository

    try:
        draft = create_final_print_outcome(
            payload,
            print_record_id=record_id,
            session_id=record.get("session_id"),
            created_by=workstation_id(request, fallback="operator") or "operator",
        )
    except ValidationError as exc:
        raise HTTPException(422, detail=str(exc)) from None

    outcome = draft.model_dump(mode="json")
    from storage.repositories.runtime import QualityOutcomeConflict

    try:
        RuntimeRepository(repo.db).save_quality_outcome(outcome)
    except QualityOutcomeConflict as exc:
        raise HTTPException(409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    from analytics.prediction.retraining import enqueue_retraining

    retraining_job = None
    if outcome.get("session_id"):
        retraining_job = enqueue_retraining(
            repo.db,
            session_id=str(outcome["session_id"]),
            outcome_id=str(outcome["outcome_id"]),
            result=str(outcome["result"]),
            timestamp=str(outcome["timestamp"]),
        )
    # Publish the dependent-row change through the existing multi-workstation
    # print-card event stream.
    repo._touch_print_record(record_id)
    # A previously generated report is a snapshot. Clear the in-process read
    # cache so its next local regeneration includes the new inspection.
    if record.get("session_id"):
        from api.routes.sessions import _invalidate_cache

        _invalidate_cache(record["session_id"])
    return {**outcome, "model_retraining_job_id": (retraining_job or {}).get("job_id")}


@router.get("/{record_id}/operator-report")
def get_print_operator_report(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Return a compact report even before a card has linked log files."""
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")

    from domain.services.operator_report import build_operator_report
    from storage.repositories.runtime import RuntimeRepository

    runtime = RuntimeRepository(repo.db)
    session_id = record.get("session_id")
    payload = runtime.get_session_payload(session_id) if session_id else None
    outcomes = runtime.list_quality_outcomes(print_record_id=record_id)
    return build_operator_report(
        session_id=session_id,
        group=(payload or {}).get("group") or {},
        quality_outcomes=outcomes,
        print_record=record,
    )


@router.patch("/{record_id}")
def update_print(
    record_id: str,
    payload: dict,
    request: Request,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Partial update: name, material, layer thickness, hatch distance, notes,
    status, session_id, printed_at, powder cost."""
    values: dict = {}
    if "name" in payload:
        name = (payload["name"] or "").strip()
        if not name:
            raise HTTPException(422, "Поле 'name' не может быть пустым")
        values["name"] = name
    if "material" in payload:
        values["material"] = _clean_material(payload["material"])
    if "layer_thickness_mm" in payload:
        values["layer_thickness_mm"] = _parse_layer_thickness(payload["layer_thickness_mm"])
    if "hatch_distance_mm" in payload:
        values["hatch_distance_mm"] = _parse_hatch_distance(payload["hatch_distance_mm"])
    if "status" in payload:
        status = (payload["status"] or "").strip().lower()
        if status not in _STATUSES:
            raise HTTPException(422, f"Недопустимый статус. Допустимы: {', '.join(sorted(_STATUSES))}")
        values["status"] = status
    if "notes" in payload:
        values["notes"] = (payload["notes"] or "").strip() or None
    if "session_id" in payload:
        current_record = repo.get_print_record(record_id)
        if current_record is None:
            raise HTTPException(404, "Карточка печати не найдена")
        _require_local_print(current_record)
        new_session_id = payload["session_id"] or None
        values["session_id"] = new_session_id
        if new_session_id and "printed_at" not in payload:
            from domain.models.sessions import BuildSession
            session = repo.db.get(BuildSession, new_session_id)
            if session is not None:
                try:
                    require_compute_owner(
                        entity_type="session",
                        entity_id=session.session_id,
                        origin_compute_node_id=session.origin_compute_node_id,
                        requested_compute_node_id=get_settings().compute_node_id,
                    )
                except ComputeAffinityError as exc:
                    raise HTTPException(
                        409,
                        "Нельзя связать карточку с сессией другого ПК. " + str(exc),
                    ) from exc
            if session and session.start_ts:
                ts = session.start_ts
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                values["printed_at"] = ts
    if "printed_at" in payload:
        values["printed_at"] = _parse_iso_datetime(payload["printed_at"], "printed_at")
    if "powder_cost_rub_per_kg" in payload:
        values["powder_cost_rub_per_kg"] = _parse_powder_cost(payload["powder_cost_rub_per_kg"])
    if not values:
        raise HTTPException(422, "Нет полей для обновления")
    actor = workstation_id(request)
    if actor:
        values["updated_by"] = actor

    if "expected_revision" not in payload:
        raise HTTPException(
            428,
            "Для изменения общей карточки обязателен expected_revision",
        )
    expected_revision = payload["expected_revision"]
    if isinstance(expected_revision, bool):
        raise HTTPException(422, "expected_revision должен быть целым положительным числом")
    try:
        expected_revision = int(expected_revision)
    except (TypeError, ValueError):
        raise HTTPException(422, "expected_revision должен быть целым положительным числом") from None
    if expected_revision < 1:
        raise HTTPException(422, "expected_revision должен быть целым положительным числом")
    try:
        record = repo.update_print_record(
            record_id,
            values,
            expected_revision=expected_revision,
        )
    except PrintRecordConflict as conflict:
        raise HTTPException(
            409,
            detail={
                "message": "Карточка уже изменена на другом рабочем месте",
                "current": conflict.current,
            },
        ) from None
    except PrintSessionLinkConflict as conflict:
        raise HTTPException(
            409,
            f"Сессия не может быть привязана к карточке: {conflict}",
        ) from None
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    repo.flush()
    # Manually linking a record to a session creates a new predicted/actual pair
    # (and, if the session has a time_log, a new recoat measurement) → refresh both.
    if values.get("session_id"):
        from analytics.prediction.retraining import enqueue_retraining_for_session
        from analytics.prediction.accuracy import (
            recalibrate_and_apply,
            try_acquire_calibration_lock,
        )
        from analytics.prediction.recoat_calibration import recalibrate_recoat_and_apply
        from analytics.prediction.scan_calibration import recalibrate_scan_and_apply
        try:
            if try_acquire_calibration_lock(repo.db):
                recalibrate_and_apply(repo.db)
                recalibrate_recoat_and_apply(repo.db)
                recalibrate_scan_and_apply(repo.db)
                repo.flush()
            else:
                logger.info("calibration already runs on another operator PC; skipped")
        except Exception:
            logger.exception("auto-calibration after manual link failed")
        try:
            enqueue_retraining_for_session(repo.db, str(values["session_id"]))
        except Exception:
            logger.exception("auto-retraining enqueue after manual link failed")
    return record


@router.delete("/{record_id}")
def delete_print(
    record_id: str,
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Delete a record with all attached files (DB rows + stored objects)."""
    if not repo.get_print_record(record_id):
        raise HTTPException(404, "Карточка печати не найдена")
    uris = repo.delete_print_record(record_id)
    repo.flush()
    # MinIO cleanup runs after get_db commits so DB and object store stay in sync
    background_tasks.add_task(_remove_objects, uris)
    logger.info("prints: deleted %s (%d files)", record_id, len(uris))
    return {"deleted": record_id, "files_removed": len(uris)}


def _remove_objects(uris: list[str]) -> None:
    """Best-effort MinIO cleanup after the DB rows are gone."""
    if not uris:
        return
    store = ObjectStore()
    for uri in uris:
        bucket, _, object_name = uri.removeprefix("s3://").partition("/")
        if not store.remove_object(bucket, object_name):
            logger.warning("prints: could not remove %s from storage", uri)


def _stage_upload_to_file(
    source: BinaryIO,
    destination: Path,
    max_bytes: int,
) -> tuple[int, str]:
    """Copy an upload to disk while hashing it and enforcing the size cap."""
    total = 0
    digest = hashlib.sha256()
    with destination.open("wb") as sink:
        while chunk := source.read(_UPLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > max_bytes:
                raise HTTPException(413, f"Файл > {max_bytes // (1024 * 1024)} МБ")
            digest.update(chunk)
            sink.write(chunk)
    return total, digest.hexdigest()


@router.post("/{record_id}/files")
async def upload_print_file(
    record_id: str,
    file: UploadFile,
    background_tasks: BackgroundTasks,
    response: Response,
    file_type: str = Form(...),
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Attach a file (STL / Magics / photo / doc) to a print record.

    The object key includes the content checksum, so same-named files with
    different content never overwrite each other; identical uploads dedupe.
    Uploading a part or support STL schedules the time/cost prediction in the
    background — both, not just parts: a part-only re-estimate silently
    undercounts scan time for any record whose supports haven't been uploaded
    yet at that moment (real supports can be a large share of scan time).
    """
    if file_type not in _FILE_TYPES:
        raise HTTPException(422, f"Недопустимый file_type. Допустимы: {', '.join(sorted(_FILE_TYPES))}")
    file_name = (file.filename or "unknown").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if len(file_name) > 300:
        raise HTTPException(422, "Имя файла слишком длинное (макс. 300 символов)")
    # MagicsX support exports use the s_ prefix — classify them automatically
    if file_type == "stl" and file_name.lower().startswith("s_"):
        file_type = "stl_supports"

    with tempfile.TemporaryDirectory(prefix="printer-upload-") as temporary_dir:
        staged_path = Path(temporary_dir) / "payload"
        await file.seek(0)
        size_bytes, checksum = await asyncio.to_thread(
            _stage_upload_to_file,
            file.file,
            staged_path,
            _MAX_UPLOAD_MB * 1024 * 1024,
        )
        if not size_bytes:
            raise HTTPException(422, "Пустой файл")

        # The browser may have opened this card before the whole NAS link went
        # down. Preserve the upload locally even when PostgreSQL cannot answer;
        # the sync worker validates existence and compute ownership before it
        # publishes anything. A reachable DB still fails fast for bad cards.
        record: dict[str, Any] | None = None
        try:
            record = repo.get_print_record(record_id)
            if not record:
                raise HTTPException(404, "Карточка печати не найдена")
            if file_type in ("stl", "stl_supports"):
                _require_local_print(record)
            existing = repo.find_file_by_checksum(record_id, checksum)
        except SQLAlchemyError as exc:
            logger.warning("prints: PostgreSQL unavailable during upload pre-check: %s", exc)
            repo.db.rollback()
            existing = None
        if existing:
            repo.db.rollback()
            return {"duplicate": True, **existing}
        # Do not hold a PostgreSQL snapshot/connection while up to 600 MB is
        # copied to the outbox and sent to MinIO. DB uniqueness handles a
        # concurrent winner later.
        repo.db.rollback()

        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        object_bucket = _bucket_for(file_type)
        settings = get_settings()
        try:
            outbox = LocalNasOutbox.from_settings(settings)
            queued = await asyncio.to_thread(
                outbox.enqueue_attachment,
                staged_path,
                owner_node_id=settings.compute_node_id,
                record_id=record_id,
                file_name=file_name,
                file_type=file_type,
                bucket=object_bucket,
                checksum=checksum,
                size_bytes=size_bytes,
                content_type=content_type,
                # The DB pre-check above found no row. A local completion
                # receipt can be stale after restoring PostgreSQL from backup.
                reopen_completed=True,
            )
        except OutboxFullError as exc:
            raise HTTPException(
                507,
                "Локальная очередь NAS заполнена; освободите место или дождитесь синхронизации",
            ) from exc
        except OSError as exc:
            raise HTTPException(
                507,
                "Не удалось сохранить локальную страховочную копию загрузки",
            ) from exc

    operation_id = str(queued["operation_id"])

    def _postponed(reason: str) -> dict[str, Any]:
        state = outbox.get(operation_id) or queued
        response.status_code = 202
        return {
            "queued": True,
            "sync_operation_id": operation_id,
            "sync_status": state.get("status", "pending"),
            "file_name": file_name,
            "file_type": file_type,
            "checksum": checksum,
            "size_bytes": size_bytes,
            "message": reason,
        }

    claimed = await asyncio.to_thread(outbox.claim, operation_id)
    if claimed is None:
        return _postponed("Файл уже находится в локальной очереди синхронизации")

    try:
        queued_path = await asyncio.to_thread(outbox.verify_claimed, claimed)
        store = ObjectStore()
        if not await asyncio.to_thread(store.is_available):
            await asyncio.to_thread(
                outbox.release,
                operation_id,
                "Хранилище файлов MinIO недоступно",
            )
            return _postponed(
                "NAS недоступен: файл сохранён на этом ПК и будет отправлен автоматически"
            )

        verified_put = getattr(store, "put_file_verified", None)
        if callable(verified_put):
            object_uri = await asyncio.to_thread(
                verified_put,
                claimed["bucket"],
                claimed["object_name"],
                queued_path,
                expected_sha256=checksum,
                expected_size=size_bytes,
                content_type=content_type,
            )
        else:
            # Compatible object-store adapters used by tests/integrations; the
            # local outbox verified checksum and size immediately above.
            object_uri = await asyncio.to_thread(
                store.put_file,
                claimed["bucket"],
                claimed["object_name"],
                queued_path,
                content_type=content_type,
            )
    except Exception as exc:
        await asyncio.to_thread(outbox.release, operation_id, str(exc))
        logger.warning("prints: upload queued after MinIO failure: %s", exc)
        return _postponed(
            "Передача на NAS прервалась: локальная копия сохранена и будет отправлена повторно"
        )

    try:
        saved = repo.add_print_file({
            "file_id": claimed["file_id"],
            "record_id": record_id,
            "object_uri": object_uri,
            "file_name": file_name,
            "file_type": file_type,
            "size_bytes": size_bytes,
            "checksum": checksum,
        })
        if saved.get("duplicate"):
            # A different workstation won the uniqueness race. Its row points
            # at another immutable URI, so this request's object is disposable.
            repo.db.rollback()
            if saved["object_uri"] != object_uri:
                removed = await asyncio.to_thread(
                    store.remove_object,
                    claimed["bucket"],
                    claimed["object_name"],
                )
                if not removed:
                    logger.warning("prints: duplicate cleanup failed for %s", object_uri)
            await asyncio.to_thread(outbox.complete, operation_id, saved)
            return saved

        # A dated file name pins down the print date when the record has none yet
        if record is not None and not record.get("printed_at"):
            from_file = _date_from_text(file_name)
            if from_file:
                repo.update_print_record(record_id, {"printed_at": from_file})
        # Деталь или поддержка → автоматический прогноз времени/стоимости в фоне,
        # чтобы пара «прогноз/факт» образовалась без ручного нажатия. Обе ветки —
        # если бы триггерилось только на "stl", загрузка поддержек уже после
        # деталей (обычный порядок ручного и массового прикрепления) молча
        # оставляла бы прогноз без них: последний срабатывавший пересчёт не видел
        # ни одной поддержки.
        should_auto_estimate = file_type in ("stl", "stl_supports")
        if should_auto_estimate:
            updated_record = repo.get_print_record(record_id)
            _enqueue_estimate(repo, updated_record)
        # Commit here, rather than after the response dependency unwinds, so a
        # failed DB publication can still remove this request's unique object.
        repo.db.commit()
    except SQLAlchemyError as exc:
        # The full object may already be on MinIO. Keep the verified local copy
        # and retry the short catalogue transaction after PostgreSQL returns.
        repo.db.rollback()
        await asyncio.to_thread(outbox.release, operation_id, str(exc))
        logger.warning("prints: database publication postponed: %s", exc)
        return _postponed(
            "База NAS временно недоступна: файл сохранён локально и будет опубликован автоматически"
        )
    except Exception as exc:
        repo.db.rollback()
        await asyncio.to_thread(outbox.fail, operation_id, str(exc))
        # Permanent application failures are quarantined for review and their
        # unreferenced remote object is removed best-effort.
        removed = await asyncio.to_thread(
            store.remove_object,
            claimed["bucket"],
            claimed["object_name"],
        )
        if not removed:
            logger.warning("prints: orphan cleanup failed for %s", object_uri)
        raise
    await asyncio.to_thread(outbox.complete, operation_id, saved)
    if should_auto_estimate and get_settings().app_env == "test":
        background_tasks.add_task(_auto_estimate, record_id)
    logger.info(
        "prints: attached %s (%s, %d bytes) to %s",
        file_name,
        file_type,
        size_bytes,
        record_id,
    )
    return saved


@router.get("/{record_id}/session-candidates")
def get_session_candidates(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Sessions near the record's print date (incl. ambiguous) for manual linking."""
    if not repo.get_print_record(record_id):
        raise HTTPException(404, "Карточка печати не найдена")
    from domain.services.print_linking import session_candidates

    return {"candidates": session_candidates(repo.db, record_id)}


@router.post("/{record_id}/import-logs")
async def import_logs_for_print(
    record_id: str,
    files: list[UploadFile],
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Upload printer logs for a specific print record.

    Files land in the raw-logs folder and go through the standard ingestion
    pipeline; auto-linking by date attaches the created session back to this
    record. A dated log file name fills the record's print date when empty.
    """
    from api.routes.uploads import _ALLOWED_SUFFIXES, _MAX_FILE_MB, _trigger_rescan

    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    _require_local_print(record)
    # The record is now a plain dict. Release the NAS read transaction before
    # copying a potentially multi-gigabyte local log batch.
    repo.db.rollback()

    settings = get_settings()
    dest = Path(settings.raw_logs_container_path)
    if not dest.exists():
        raise HTTPException(500, f"Папка логов не найдена: {dest}")

    saved, skipped = [], []
    batch_dir: Path | None = None
    printed_at_hint = None
    for f in files:
        name = Path(f.filename or "unknown").name
        if Path(name).suffix.lower() not in _ALLOWED_SUFFIXES:
            skipped.append({"name": name, "reason": "неподдерживаемый тип файла"})
            continue
        if batch_dir is None:
            batch_dir = dest / "incoming" / (
                f"print_{record_id}_"
                + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
            )
            batch_dir.mkdir(parents=True, exist_ok=False)
        total = 0
        target = batch_dir / name
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
                        target = batch_dir / f"{Path(name).stem}__{checksum[:12]}{Path(name).suffix}"
                # Cross-device copy (tmpfs -> bind mount) of up to 2 GB.
                if not duplicate:
                    await asyncio.to_thread(shutil.move, tmp_path, target)
                saved.append({
                    "name": name,
                    "stored_name": target.name,
                    "size_bytes": total,
                    "checksum": checksum,
                    "duplicate": duplicate,
                })
                printed_at_hint = printed_at_hint or _date_from_text(name)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    updates: dict = {}
    if not record.get("printed_at") and printed_at_hint:
        updates["printed_at"] = printed_at_hint
    if printed_at_hint:
        # Explicit intent: these logs belong to THIS record — the auto-linker
        # resolves the hint even when another record matches the same date.
        meta = dict(record.get("metadata_json") or {})
        meta["log_import_hint"] = {"date": printed_at_hint.date().isoformat()}
        updates["metadata_json"] = meta
    jobs = _trigger_rescan(
        settings.raw_logs_container_path,
        candidates=[batch_dir] if batch_dir is not None else [],
        db=repo.db,
        print_record_id=record_id,
    ) if saved else []
    if updates:
        repo.update_print_record(record_id, updates)
    logger.info("prints: %d log file(s) uploaded for %s", len(saved), record_id)
    return {
        "saved": saved,
        "skipped": skipped,
        "jobs": jobs,
        "note": "Подтвердите импорт в верхней панели; затем сессия привяжется к карточке по дате.",
    }


@router.delete("/{record_id}/files/{file_id}")
def delete_print_file(
    record_id: str,
    file_id: str,
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Detach one file from a record (DB row + stored object)."""
    uri = repo.delete_print_file(record_id, file_id)
    if uri is None:
        raise HTTPException(404, "Файл не найден")
    repo.flush()
    # MinIO cleanup runs after get_db commits so DB and object store stay in sync
    background_tasks.add_task(_remove_objects, [uri])
    return {"deleted": file_id}


@router.get("/{record_id}/files/{file_id}/download")
def download_print_file(
    record_id: str,
    file_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
):
    """Stream a stored file back (used by the dashboard STL viewer).

    Streamed in chunks rather than read whole: attachments are capped at 600 MB
    and the api container at 4 GB across 2 workers, so a couple of concurrent
    downloads of large STLs could exhaust it.
    """
    from fastapi.responses import StreamingResponse

    files = repo.list_print_files(record_id)
    match = next((f for f in files if f["file_id"] == file_id), None)
    if not match:
        raise HTTPException(404, "Файл не найден")

    uri = match["object_uri"]  # s3://bucket/object_name
    bucket, _, object_name = uri.removeprefix("s3://").partition("/")
    stream = ObjectStore().open_stream(bucket, object_name)
    if stream is None:
        raise HTTPException(503, "Файл недоступен в хранилище")

    content_type = mimetypes.guess_type(match["file_name"])[0] or "application/octet-stream"
    headers = {"Content-Disposition": _content_disposition(match["file_name"])}
    if match.get("size_bytes"):
        headers["Content-Length"] = str(match["size_bytes"])
    return StreamingResponse(stream, media_type=content_type, headers=headers)
