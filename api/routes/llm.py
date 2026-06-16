from fastapi import APIRouter, Depends

from api.deps.repositories import get_runtime_repository
from core.config.settings import get_settings
from reporting.llm.discovery import discover_lmstudio
from reporting.llm.evidence_package import build_evidence_package
from reporting.llm.providers.factory import get_llm_provider
from reporting.llm.providers.lmstudio import LMStudioProvider
from storage.repositories.runtime import RuntimeRepository


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
def get_report(report_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return repo.get_report(report_id) or {"error": "not_found"}


@reports_router.post("/{report_id}/llm-enhance")
async def llm_enhance_report(
    report_id: str,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    report = repo.get_report(report_id)
    if not report:
        return {"error": "not_found"}
    evidence = build_evidence_package(report).model_dump(mode="json")
    result = await get_llm_provider().generate_markdown(evidence)
    if result.success:
        report["llm_markdown"] = result.content
    report.setdefault("llm_runs", []).append(result.__dict__)
    repo.save_report(report)
    repo.flush()
    return {"report_id": report_id, "llm": result.__dict__}
