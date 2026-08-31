from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps.repositories import get_runtime_repository
from api.pagination import LimitParam, PaginatedResponse, SkipParam
from api.workstations import workstation_id
from core.config.settings import get_settings
from domain.services.import_jobs import (
    ImportExecutionResult,
    ImportJobRecord,
    detect_import_candidate,
    ignore_import_job,
    mark_import_job_confirmed,
    postpone_import_job,
    queue_import_job_retry,
)
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("")
def list_imports(
    skip: SkipParam = 0,
    limit: LimitParam = 100,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    node_id = get_settings().compute_node_id
    total = repo.count_import_jobs(owner_node_id=node_id)
    jobs = repo.list_import_jobs(owner_node_id=node_id, skip=skip, limit=limit)
    items = [job.model_dump(mode="json") for job in jobs]
    return PaginatedResponse(items=items, total=total, skip=skip, limit=limit).to_dict()


@router.get("/{import_job_id}")
def get_import(import_job_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return _get_job(import_job_id, repo).model_dump(mode="json")


@router.post("/{import_job_id}/confirm")
def confirm_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    actor = workstation_id(request, (payload or {}).get("actor", "operator"))
    job = _get_job(import_job_id, repo, for_update=True)
    _assert_not_running(job)
    result = mark_import_job_confirmed(job, actor=actor)
    _persist_result(result, repo)
    return _response(result)


@router.post("/{import_job_id}/ignore")
def ignore_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    actor = workstation_id(request, (payload or {}).get("actor", "operator"))
    job = _get_job(import_job_id, repo, for_update=True)
    _assert_not_running(job)
    result = ignore_import_job(job, actor=actor)
    _persist_result(result, repo)
    return _response(result)


@router.post("/{import_job_id}/postpone")
def postpone_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    payload = payload or {}
    job = _get_job(import_job_id, repo, for_update=True)
    _assert_not_running(job)
    result = postpone_import_job(
        job,
        retry_seconds=payload.get("retry_seconds"),
        actor=workstation_id(request, payload.get("actor", "operator")),
        settings=get_settings(),
    )
    _persist_result(result, repo)
    return _response(result)


@router.post("/{import_job_id}/retry")
def retry_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    actor = workstation_id(request, (payload or {}).get("actor", "operator"))
    job = _get_job(import_job_id, repo, for_update=True)
    _assert_not_running(job)
    result = queue_import_job_retry(job, actor=actor)
    _persist_result(result, repo)
    return _response(result)


def create_detected_import(
    source_path: str,
    repo: RuntimeRepository,
    *,
    print_record_id: str | None = None,
) -> ImportExecutionResult:
    settings = get_settings()
    incoming = Path(source_path).resolve(strict=False)
    source_path = str(incoming)
    incoming_name = incoming.name
    from domain.services.import_jobs import (
        calculate_checksum_manifest,
        snapshot_source,
    )
    from storage.db.session import SessionLocal

    # All filesystem work happens before the transaction-scoped advisory lock.
    # A 10 GB batch must never keep a PostgreSQL transaction open on the NAS
    # while this operator PC walks or hashes its local files.
    incoming_snapshot = snapshot_source(incoming)
    with SessionLocal() as probe_db:
        has_name_candidate = RuntimeRepository(probe_db).has_terminal_import_job_by_name(
            owner_node_id=settings.compute_node_id,
            source_name=incoming_name,
        )
    incoming_manifest = (
        calculate_checksum_manifest(incoming)
        if has_name_candidate and incoming.exists()
        else None
    )

    repo.lock_import_candidate(settings.compute_node_id, source_path)
    for existing in repo.list_import_jobs_by_source_path(
        owner_node_id=settings.compute_node_id,
        source_path=source_path,
    ):
        # Exact path match on the same owner/operator node. A terminal job is reusable only
        # while the file snapshot still matches: operators often copy a new log
        # over an old filename, which must become a new import job.
        if existing.source_path == source_path:
            # The candidate-level advisory lock prevents a second insert; the
            # row lock serializes this stronger card link with worker claim and
            # preserves any lease/fence fields that changed since list().
            current = repo.get_import_job_for_update(existing.import_job_id)
            if current is None:
                continue
            existing = current
            terminal = existing.status in ("done", "needs_operator_context", "ignored", "failed")
            if not terminal or existing.file_snapshot == incoming_snapshot:
                # The filesystem watcher may observe the newly-created batch
                # directory milliseconds before the print-specific upload
                # endpoint finishes. Preserve the endpoint's stronger,
                # explicit card identity on that already-created job.
                if print_record_id and existing.print_record_id is None:
                    existing.print_record_id = print_record_id
                    repo.save_import_job(existing)
                    repo.flush()
                elif (
                    print_record_id
                    and existing.print_record_id
                    and existing.print_record_id != print_record_id
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="Import batch is already attached to another print record",
                    )
                return ImportExecutionResult(job=existing, notifications=[])
    # Name + checksum match: same file arrived from a different path (e.g.
    # Mac→Windows). This is a narrowly filtered query, not the owner's entire
    # import history, and the expensive manifest was already calculated above.
    if incoming_manifest is not None:
        for existing in repo.list_terminal_import_jobs_by_name(
            owner_node_id=settings.compute_node_id,
            source_name=incoming_name,
        ):
            if (
                existing.checksum_manifest
                and incoming_manifest == existing.checksum_manifest
            ):
                return ImportExecutionResult(job=existing, notifications=[])
    result = detect_import_candidate(
        Path(source_path),
        settings=settings,
        print_record_id=print_record_id,
        file_snapshot=incoming_snapshot,
    )
    _persist_result(result, repo)
    return result


def handle_import_callback(callback_data: str, repo: RuntimeRepository, actor: str = "operator") -> ImportExecutionResult:
    try:
        prefix, import_job_id, action = callback_data.split(":", 2)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid callback data") from exc
    if prefix != "import":
        raise HTTPException(status_code=400, detail="Unsupported callback prefix")
    job = _get_job(import_job_id, repo, for_update=True)
    _assert_not_running(job)
    if action == "confirm":
        result = mark_import_job_confirmed(job, actor=actor)
    elif action == "ignore":
        result = ignore_import_job(job, actor=actor)
    elif action == "postpone":
        result = postpone_import_job(job, actor=actor, settings=get_settings())
    elif action == "retry":
        result = queue_import_job_retry(job, actor=actor)
    else:
        raise HTTPException(status_code=400, detail="Unsupported import callback action")
    _persist_result(result, repo)
    return result


def _get_job(
    import_job_id: str,
    repo: RuntimeRepository,
    *,
    for_update: bool = False,
) -> ImportJobRecord:
    job = (
        repo.get_import_job_for_update(import_job_id)
        if for_update
        else repo.get_import_job(import_job_id)
    )
    if not job or job.owner_node_id != get_settings().compute_node_id:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job


def _assert_not_running(job: ImportJobRecord) -> None:
    lease_until = job.lease_until
    if lease_until is None or not job.lease_owner:
        return
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    if lease_until > datetime.now(timezone.utc):
        raise HTTPException(
            status_code=409,
            detail="Import is already running on its owner PC; wait for it to finish",
        )


def _persist_result(result: ImportExecutionResult, repo: RuntimeRepository) -> None:
    repo.save_import_job(result.job)
    repo.save_notifications(result.notifications)
    repo.save_sessions(result.sessions)
    repo.save_reports(result.reports)
    repo.flush()


def _response(result: ImportExecutionResult) -> dict:
    return {
        "job": result.job.model_dump(mode="json"),
        "notifications": [notification.model_dump(mode="json") for notification in result.notifications],
        "session_ids": result.job.session_ids,
        "report_ids": result.job.report_ids,
    }
