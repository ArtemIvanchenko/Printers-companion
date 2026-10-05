"""Print archive endpoints: print record CRUD, search and file attachments."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

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
from sqlalchemy import func, select

from api.deps.repositories import get_prints_repository
from api.pagination import LimitParam, PaginatedResponse, SkipParam
from api.workstations import workstation_id
from core.config.settings import get_settings
from domain.services.estimation import requests as _estimation_requests
from domain.services.estimation.contracts import EstimateError
from domain.services.importing import uploads as _log_uploads
from domain.services.print_cards import cards as _card_services
from domain.services.print_cards import attachments as _attachments
from domain.services.print_cards.comparison import attach_plan_vs_fact
from domain.services.print_cards import quality as _card_quality
from domain.services.print_cards.contracts import CardError
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
                  "storage_unavailable": 503, "log_directory_unavailable": 500,
                  "lease_lost": 409}[exc.code]
        raise HTTPException(status, exc.detail) from None


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
    records, total = _card_call(
        _card_services.list_cards, repo, skip=skip, limit=limit, query=q,
        material=material, date_from=date_from, date_to=date_to, has_logs=has_logs,
    )
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
_PRINT_SYNC_STATE: dict[str, Any] | None = None


async def _print_sync_monitor() -> None:
    """One NAS poller per local API process, fanned out to every browser tab."""
    global _PRINT_SYNC_TASK, _PRINT_SYNC_STATE
    idle_delay = 2.0
    try:
        while _PRINT_SYNC_SUBSCRIBERS:
            try:
                current = await asyncio.to_thread(_print_sync_state)
                if current != _PRINT_SYNC_STATE:
                    _PRINT_SYNC_STATE = current
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
        _PRINT_SYNC_STATE = None


@router.get("/events")
async def print_events(request: Request) -> StreamingResponse:
    """Notify open operator screens when the shared print archive changes.

    PostgreSQL remains the source of truth; this stream only invalidates local
    browser views.  Polling a three-value aggregate also works during the NAS
    migration before a dedicated message broker is exposed centrally.
    """
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=1)

    async def stream():
        global _PRINT_SYNC_TASK, _PRINT_SYNC_STATE
        baseline = None
        _PRINT_SYNC_SUBSCRIBERS.add(queue)
        if _PRINT_SYNC_TASK is None or _PRINT_SYNC_TASK.done():
            _PRINT_SYNC_STATE = None
            _PRINT_SYNC_TASK = asyncio.create_task(_print_sync_monitor())
        else:
            baseline = _PRINT_SYNC_STATE
        try:
            if baseline is not None:
                # Prime outside the bounded change queue: coalescing must not
                # replace the baseline with the first actual card change.
                yield "event: print-records\ndata: " + json.dumps(baseline) + "\n\n"
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


@router.post("/{record_id}/estimate")
def estimate_print_record(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Enqueue a durable owner-local calculation, identically in every environment.

    Only the estimate worker downloads/slices geometry and publishes a fenced
    snapshot. The caller polls the job or GET /prints/{id} for its result.
    """
    record = repo.get_print_record(record_id)
    if not record:
        raise HTTPException(404, "Карточка печати не найдена")
    # Fail fast on the cheap preconditions so the operator hears about a
    # missing STL or an unfilled parameter now, not after a silent no-op.
    _card_call(_estimation_requests.assert_estimatable, repo, record,
               compute_node_id=get_settings().compute_node_id)

    job = _card_call(_estimation_requests.enqueue_estimate, repo, record,
                     force=True, compute_node_id=get_settings().compute_node_id)
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
    record = _card_call(_card_services.get_card, repo, record_id)
    attach_plan_vs_fact(repo, [record])
    return record


@router.get("/{record_id}/quality-outcomes")
def list_print_quality_outcomes(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> list[dict]:
    return _card_call(_card_quality.list_quality_outcomes, repo, record_id)


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
    outcome = _card_call(
        _card_quality.create_quality_outcome, repo, record_id, payload,
        actor=workstation_id(request, fallback="operator") or "operator",
    )
    # Publication has committed. A failed transaction must not invalidate the
    # cache, and regeneration must be able to observe the new inspection.
    if outcome.get("session_id"):
        from api.routes.sessions import _invalidate_cache

        _invalidate_cache(outcome["session_id"])
    return outcome


@router.get("/{record_id}/operator-report")
def get_print_operator_report(
    record_id: str,
    repo: PrintsRepository = Depends(get_prints_repository),
) -> dict:
    """Return a compact report even before a card has linked log files."""
    return _card_call(_card_quality.get_operator_report, repo, record_id)


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
    background_tasks.add_task(
        _attachments.remove_unreferenced_objects, result.object_uris, store_factory=ObjectStore,
    )
    return result.payload


@router.post("/{record_id}/files")
async def upload_print_file(
    record_id: str,
    file: UploadFile,
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
    result = await asyncio.to_thread(
        _card_call, _log_uploads.upload_log_batch, repo,
        [_log_uploads.LogUpload(file.filename, file.file) for file in files],
        settings=get_settings(), record_id=record_id, max_file_mb=_log_uploads.MAX_FILE_MB,
    )
    return {
        **result,
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
    background_tasks.add_task(
        _attachments.remove_unreferenced_objects, result.object_uris, store_factory=ObjectStore,
    )
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
