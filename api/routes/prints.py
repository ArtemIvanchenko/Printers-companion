"""Print archive endpoints: print record CRUD, search and file attachments."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import multiprocessing
import os
import shutil
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timezone
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

from api.deps.repositories import get_prints_repository
from api.pagination import LimitParam, PaginatedResponse, SkipParam
from api.workstations import workstation_id
from core.config.settings import get_settings
from domain.services.estimation import calculation as _estimation_calculation
from domain.services.estimation import inputs as _estimation_inputs
from domain.services.estimation import publication as _estimation_publication
from domain.services.estimation import requests as _estimation_requests
from domain.services.estimation.contracts import EstimateError
from domain.services.print_cards import cards as _card_services
from domain.services.print_cards import attachments as _attachments
from domain.services.print_cards import validation as _card_validation
from domain.services.print_cards.contracts import CardError
from parsers.common.timestamps import date_hint_datetime
from storage.object_store.minio_client import ObjectStore
from storage.repositories.prints_repo import PrintsRepository

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prints", tags=["prints"])

_MAX_UPLOAD_MB = 600
# Materials offered when machine_params has no densities configured yet
_DEFAULT_MATERIALS = ["steel", "aluminum", "titanium", "other"]


def _card_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (CardError, EstimateError) as exc:
        status = {"invalid_inputs": 422, "not_found": 404, "conflict": 409,
                  "stale_inputs": 409, "forbidden": 403, "precondition_required": 428,
                  "too_large": 413, "insufficient_storage": 507,
                  "storage_unavailable": 503}[exc.code]
        raise HTTPException(status, exc.detail) from None


def _require_local_print(record: dict, *, compute_node_id: str | None = None) -> None:
    _estimate_call(_estimation_inputs.require_local_print, record,
                   compute_node_id=compute_node_id or get_settings().compute_node_id)


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
    return _attachments.bucket_for(file_type, get_settings())


def _clean_material(raw: str | None) -> str:
    return _card_call(_card_validation.clean_material, raw)


def _parse_iso_datetime(raw, field: str) -> datetime | None:
    return _card_call(_card_validation.parse_iso_datetime, raw, field)


def _parse_powder_cost(raw) -> float | None:
    return _card_call(_card_validation.parse_powder_cost, raw)


def _parse_layer_thickness(raw) -> float | None:
    return _card_call(_card_validation.parse_layer_thickness, raw)


def _parse_hatch_distance(raw) -> float | None:
    return _card_call(_card_validation.parse_hatch_distance, raw)


def _date_from_text(text: str) -> datetime | None:
    """Print date hint from a record/file name like '23.03.2026_кронштейн'."""
    return date_hint_datetime(text)


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
    return _card_call(_card_services.create_card, repo, payload,
                      compute_node_id=get_settings().compute_node_id, actor=workstation_id(request))


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


@router.get("/latest-prediction")
def get_latest_prediction(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """One historical estimate, even when the newest catalogue page has none."""
    return {"record": repo.latest_prediction_record()}


@router.get("/prediction-accuracy")
def get_prediction_accuracy(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Describe shared evidence without training a model during an HTTP read."""
    from analytics.prediction.calibration import accuracy_view, latest_calibration_report
    from analytics.prediction.calibration_inputs import load_calibration_inputs

    inputs = load_calibration_inputs(repo.db)
    published = latest_calibration_report(repo.db)
    # This GET has no writes. Release its read transaction before statistics;
    # the request unit of work remains responsible for final cleanup.
    repo.db.rollback()
    return accuracy_view(inputs, published)


@router.post("/recalibrate", status_code=202)
def recalibrate(repo: PrintsRepository = Depends(get_prints_repository)) -> dict:
    """Contract v2: accepted owner-local job, not an already applied result."""
    from analytics.prediction.calibration import CONTRACT_VERSION, enqueue_calibration

    job = enqueue_calibration(repo.db, owner_node_id=get_settings().compute_node_id, trigger="operator")
    repo.flush()
    return {"contract_version": CONTRACT_VERSION, "status": job["status"],
            "job_id": job["job_id"], "owner_node_id": job["owner_node_id"],
            "message": "Калибровка поставлена в очередь на этом ПК.",
            "applied": {}, "skipped": [], "recoat": {"applied": {}}, "scan": {"applied": {}}}


# Compatibility adapters: HTTP mapping and request-local dependencies only.
# The durable worker imports the application services, never these routes.
def _estimate_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except EstimateError as exc:
        status = {"forbidden": 403, "not_found": 404, "stale_inputs": 409,
                  "invalid_inputs": 422, "storage_unavailable": 503, "lease_lost": 409}[exc.code]
        raise HTTPException(status, exc.detail) from None


_combined_prediction = _estimation_calculation.combined_prediction
_calibration_mismatch_warning = _estimation_calculation._calibration_mismatch_warning
_enrich_prediction_interval = _estimation_calculation.enrich_prediction_interval
params_for_record = _estimation_inputs.params_for_record
_params_with_sources_for_record = _estimation_inputs._params_with_sources_for_record
_geometry_quality = _estimation_inputs._geometry_quality
_geometry_fingerprint = _estimation_inputs.geometry_fingerprint
_prediction_input_hash = _estimation_inputs.prediction_input_hash


def _assert_geometry_usable(record: dict) -> None:
    _estimate_call(_estimation_inputs._assert_geometry_usable, record)


def _prepare_prediction_inputs(repo, record_id, *, compute_node_id=None):
    return _estimate_call(_estimation_inputs.prepare_prediction_inputs, repo, record_id,
                         compute_node_id=compute_node_id or get_settings().compute_node_id)


def _calculate_prediction_snapshot(prepared, **kwargs):
    return _estimate_call(_estimation_calculation.calculate_prediction_snapshot, prepared,
                         object_store_factory=ObjectStore, plate_calculator=_combined_prediction, **kwargs)


def _store_prediction_snapshot(repo, record_id, snapshot, *, expected_revision=None, compute_node_id=None):
    return _estimate_call(_estimation_publication.store_prediction_snapshot, repo, record_id, snapshot,
                         expected_revision=expected_revision,
                         compute_node_id=compute_node_id or get_settings().compute_node_id)


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


def _assert_estimatable(repo, record):
    _estimate_call(_estimation_requests.assert_estimatable, repo, record,
                   compute_node_id=get_settings().compute_node_id)


def _enqueue_estimate(repo, record, *, force=False):
    return _estimate_call(_estimation_requests.enqueue_estimate, repo, record,
                         force=force, compute_node_id=get_settings().compute_node_id)


# PLAN_ACCURACY.md 2.4. One worker: compute_layer_series already parallelises
# internally across 4 threads (layer_engine._SECTION_THREADS) up to this
# container's own CPU limit — a second concurrent estimate would only fight
# the first one for the same cores, not add real throughput. A second request
# just queues behind it in the pool rather than racing it.
_ESTIMATE_POOL: ProcessPoolExecutor | None = None


def _estimate_pool() -> ProcessPoolExecutor:
    global _ESTIMATE_POOL
    if _ESTIMATE_POOL is None:
        # Never inherit the API's live SQLAlchemy sockets, native geometry
        # threads or patched module state via Linux's Python 3.11 fork default.
        # A fresh interpreter also matches Windows/macOS operator behaviour.
        _ESTIMATE_POOL = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn"),
        )
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
    return _card_call(_card_services.get_card, repo, record_id)


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
    return _card_call(_card_services.update_card, repo, record_id, payload,
                      compute_node_id=get_settings().compute_node_id, actor=workstation_id(request))


@router.delete("/{record_id}")
def delete_print(
    record_id: str,
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Delete a record with all attached files (DB rows + stored objects)."""
    result = _card_call(_card_services.delete_card, repo, record_id)
    background_tasks.add_task(_remove_objects, result.object_uris)
    return result.payload


def _remove_objects(uris: list[str]) -> None:
    """Compatibility adapter; the delete service has already committed."""
    _attachments.remove_unreferenced_objects(uris, store_factory=ObjectStore)


def _stage_upload_to_file(
    source: BinaryIO,
    destination: Path,
    max_bytes: int,
) -> tuple[int, str]:
    return _card_call(_attachments.stage_upload_to_file, source, destination, max_bytes)


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
    result = await asyncio.to_thread(
        _card_call, _attachments.upload_attachment, repo, record_id, file.file,
        file_name=file.filename, file_type=file_type, settings=get_settings(),
        store_factory=ObjectStore, max_bytes=_MAX_UPLOAD_MB * 1024 * 1024,
    )
    if result.queued:
        response.status_code = 202
    if result.new_geometry and get_settings().app_env == "test":
        background_tasks.add_task(_auto_estimate, record_id)
    return result.payload


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
    pipeline; the card-specific import job attaches its resulting session back
    to this record. A dated filename only hints at the print date when empty.
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
        # Date is a display/candidate hint, not proof of log identity. The
        # durable card-specific job below provides the actual session lineage.
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
        "note": "Подтвердите импорт в верхней панели; одна сессия из этого задания привяжется к карточке. Если сессий несколько, потребуется выбор.",
    }


@router.delete("/{record_id}/files/{file_id}")
def delete_print_file(
    record_id: str,
    file_id: str,
    background_tasks: BackgroundTasks,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Detach one file from a record (DB row + stored object)."""
    result = _card_call(_attachments.delete_attachment, repo, record_id, file_id)
    background_tasks.add_task(_remove_objects, result.object_uris)
    return result.payload


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
    result = _card_call(_attachments.download_attachment, repo, record_id, file_id,
                        store_factory=ObjectStore)
    headers = {"Content-Disposition": _content_disposition(result.file_name)}
    if result.size_bytes:
        headers["Content-Length"] = str(result.size_bytes)
    return StreamingResponse(result.stream, media_type=result.content_type, headers=headers)
