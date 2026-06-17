from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from api.pagination import LimitParam, PaginatedResponse, SkipParam
from core.config.settings import get_settings
from domain.services.import_jobs import (
    ImportExecutionResult,
    ImportJobRecord,
    detect_import_candidate,
    ignore_import_job,
    mark_import_job_confirmed,
    postpone_import_job,
    retry_import_job,
)
from profiles.m350.profile import build_registry, get_profile
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("")
def list_imports(
    skip: SkipParam = 0,
    limit: LimitParam = 100,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    all_jobs = repo.list_import_jobs()
    total = len(all_jobs)
    items = [j.model_dump(mode="json") for j in all_jobs[skip:skip + limit]]
    return PaginatedResponse(items=items, total=total, skip=skip, limit=limit).to_dict()


@router.get("/{import_job_id}")
def get_import(import_job_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return _get_job(import_job_id, repo).model_dump(mode="json")


@router.post("/{import_job_id}/confirm")
def confirm_import(
    import_job_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    actor = (payload or {}).get("actor", "operator")
    result = mark_import_job_confirmed(_get_job(import_job_id, repo), actor=actor)
    _persist_result(result, repo)
    return _response(result)


@router.post("/{import_job_id}/ignore")
def ignore_import(
    import_job_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    actor = (payload or {}).get("actor", "operator")
    result = ignore_import_job(_get_job(import_job_id, repo), actor=actor)
    _persist_result(result, repo)
    return _response(result)


@router.post("/{import_job_id}/postpone")
def postpone_import(
    import_job_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    payload = payload or {}
    result = postpone_import_job(
        _get_job(import_job_id, repo),
        retry_seconds=payload.get("retry_seconds"),
        actor=payload.get("actor", "operator"),
        settings=get_settings(),
    )
    _persist_result(result, repo)
    return _response(result)


@router.post("/{import_job_id}/retry")
def retry_import(
    import_job_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    actor = (payload or {}).get("actor", "operator")
    result = retry_import_job(
        _get_job(import_job_id, repo),
        registry=build_registry(),
        profile=get_profile(),
        actor=actor,
        settings=get_settings(),
    )
    _persist_result(result, repo)
    return _response(result)


def create_detected_import(source_path: str, repo: RuntimeRepository) -> ImportExecutionResult:
    incoming = Path(source_path)
    incoming_name = incoming.name
    for existing in repo.list_import_jobs():
        # Exact path match (same machine)
        if existing.source_path == source_path:
            return ImportExecutionResult(job=existing, notifications=[])
        # Name + checksum match: same file arrived from a different path (e.g. Mac→Windows)
        if existing.source_name == incoming_name and existing.status in ("done", "needs_operator_context"):
            if existing.checksum_manifest and incoming.exists():
                from domain.services.import_jobs import calculate_checksum_manifest
                incoming_manifest = calculate_checksum_manifest(incoming)
                if incoming_manifest == existing.checksum_manifest:
                    return ImportExecutionResult(job=existing, notifications=[])
    result = detect_import_candidate(Path(source_path), settings=get_settings())
    _persist_result(result, repo)
    return result


def handle_import_callback(callback_data: str, repo: RuntimeRepository, actor: str = "operator") -> ImportExecutionResult:
    try:
        prefix, import_job_id, action = callback_data.split(":", 2)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid callback data") from exc
    if prefix != "import":
        raise HTTPException(status_code=400, detail="Unsupported callback prefix")
    job = _get_job(import_job_id, repo)
    registry = build_registry()
    profile = get_profile()
    if action == "confirm":
        result = mark_import_job_confirmed(job, actor=actor)
    elif action == "ignore":
        result = ignore_import_job(job, actor=actor)
    elif action == "postpone":
        result = postpone_import_job(job, actor=actor, settings=get_settings())
    elif action == "retry":
        result = retry_import_job(job, registry=registry, profile=profile, actor=actor, settings=get_settings())
    else:
        raise HTTPException(status_code=400, detail="Unsupported import callback action")
    _persist_result(result, repo)
    return result


def _get_job(import_job_id: str, repo: RuntimeRepository) -> ImportJobRecord:
    job = repo.get_import_job(import_job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return job


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
