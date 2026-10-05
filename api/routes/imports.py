from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from api.pagination import LimitParam, PaginatedResponse, SkipParam
from api.workstations import workstation_id
from core.config.settings import get_settings
from domain.services.importing.contracts import ImportExecutionResult
from domain.services.importing import requests as import_requests
from storage.db.session import get_db
from storage.repositories.import_jobs import ImportJobsRepository


router = APIRouter(prefix="/imports", tags=["imports"])


@router.get("")
def list_imports(
    skip: SkipParam = 0,
    limit: LimitParam = 100,
    db: Session = Depends(get_db),
) -> dict:
    repo = ImportJobsRepository(db)
    node_id = get_settings().compute_node_id
    total = repo.count_import_jobs(owner_node_id=node_id)
    jobs = repo.list_import_jobs(owner_node_id=node_id, skip=skip, limit=limit)
    items = [job.model_dump(mode="json") for job in jobs]
    return PaginatedResponse(items=items, total=total, skip=skip, limit=limit).to_dict()


@router.get("/{import_job_id}")
def get_import(import_job_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return import_requests.get_import_job(db, import_job_id, settings=get_settings()).model_dump(mode="json")
    except import_requests.ImportRequestError as exc:
        raise _http_error(exc) from exc


@router.post("/{import_job_id}/confirm")
def confirm_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
) -> dict:
    actor = workstation_id(request, (payload or {}).get("actor", "operator"))
    result = _apply_action(import_job_id, "confirm", db, actor=actor)
    return _response(result)


@router.post("/{import_job_id}/ignore")
def ignore_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
) -> dict:
    actor = workstation_id(request, (payload or {}).get("actor", "operator"))
    result = _apply_action(import_job_id, "ignore", db, actor=actor)
    return _response(result)


@router.post("/{import_job_id}/postpone")
def postpone_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
) -> dict:
    payload = payload or {}
    result = _apply_action(
        import_job_id, "postpone", db,
        retry_seconds=payload.get("retry_seconds"),
        actor=workstation_id(request, payload.get("actor", "operator")),
    )
    return _response(result)


@router.post("/{import_job_id}/retry")
def retry_import(
    import_job_id: str,
    request: Request,
    payload: dict | None = None,
    db: Session = Depends(get_db),
) -> dict:
    actor = workstation_id(request, (payload or {}).get("actor", "operator"))
    result = _apply_action(import_job_id, "retry", db, actor=actor)
    return _response(result)


def _apply_action(
    import_job_id: str,
    action: str,
    db: Session,
    *,
    actor: str,
    retry_seconds: int | None = None,
) -> ImportExecutionResult:
    try:
        return import_requests.apply_import_action(
            db, import_job_id, action, settings=get_settings(), actor=actor, retry_seconds=retry_seconds,
        )
    except import_requests.ImportRequestError as exc:
        raise _http_error(exc) from exc


def _http_error(error: import_requests.ImportRequestError) -> HTTPException:
    status = {"not_found": 404, "conflict": 409, "invalid_inputs": 400}[error.code]
    return HTTPException(status, error.detail)


def _response(result: ImportExecutionResult) -> dict:
    return {
        "job": result.job.model_dump(mode="json"),
        "notifications": [notification.model_dump(mode="json") for notification in result.notifications],
        "session_ids": result.job.session_ids,
        "report_ids": result.job.report_ids,
    }
