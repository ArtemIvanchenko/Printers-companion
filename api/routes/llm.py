from fastapi import APIRouter, Depends, HTTPException

from core.config.settings import get_settings
from domain.services import session_reports
from reporting.llm.discovery import discover_lmstudio
from reporting.llm.providers.factory import get_llm_provider
from reporting.llm.providers.lmstudio import LMStudioProvider
from storage.db.session import get_db
from sqlalchemy.orm import Session


router = APIRouter(prefix="/llm", tags=["llm"])
reports_router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/status")
def llm_status() -> dict:
    return get_llm_provider().status()


@router.get("/health")
async def llm_health() -> dict:
    """Live reachability check of the currently configured LM Studio server."""
    provider = get_llm_provider()
    if isinstance(provider, LMStudioProvider):
        return await provider.health()
    return {**provider.status(), "reachable": None}


@router.post("/discover")
async def llm_discover() -> dict:
    """Actively probe for a running LM Studio server and auto-connect if found.

    On success the live settings are updated (base URL + loaded model), so all
    subsequent report generations use the discovered server immediately.
    """
    settings = get_settings()
    result = await discover_lmstudio(preferred_model=settings.llm_model)
    if result.available:
        settings.llm_base_url = result.base_url
        if result.selected_model:
            settings.llm_model = result.selected_model
    return result.to_dict()


@router.post("/test")
async def llm_test(payload: dict | None = None) -> dict:
    provider = get_llm_provider()
    evidence = {"test": True, "payload": payload or {}}
    result = await provider.generate_markdown(evidence)
    return result.__dict__


@router.get("/providers")
def llm_providers() -> list[dict]:
    return [
        {"provider": "lmstudio", "default": True, "openai_compatible": True},
        {"provider": "null", "default": False, "openai_compatible": False},
    ]


@reports_router.get("/{report_id}")
def get_report(report_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return session_reports.read_report_by_id(db, report_id)
    except session_reports.SessionReportError as exc:
        raise _report_error(exc) from exc


def _report_error(exc: session_reports.SessionReportError) -> HTTPException:
    return HTTPException(status_code={"not_found": 404, "conflict": 409,
                                     "forbidden": 403, "storage_unavailable": 503}[exc.code],
                         detail=exc.detail)


@reports_router.post("/{report_id}/llm-enhance")
async def llm_enhance_report(
    report_id: str,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return await session_reports.enhance_report(db, report_id,
            compute_node_id=get_settings().compute_node_id)
    except session_reports.SessionReportError as exc:
        raise _report_error(exc) from exc
