from fastapi import APIRouter, Depends

from api.deps.repositories import get_runtime_repository
from background_reanalysis.job_planner import plan_historical_reanalysis
from background_reanalysis.scheduler import run_bounded_historical_reanalysis
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/background-analysis", tags=["background-analysis"])


@router.post("/run")
def run_background_analysis(
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    payload = payload or {}
    plan = plan_historical_reanalysis(
        window_days=int(payload.get("window_days", 90)),
        max_iterations=int(payload.get("max_iterations", 10)),
    )
    features = payload.get("session_features", [])
    verdict = run_bounded_historical_reanalysis(plan, features)
    repo.save_historical_verdict(verdict)
    repo.flush()
    return verdict


@router.get("/status")
def get_background_status(repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return {
        "daily_review": "scheduled",
        "historical_reanalysis": "bounded",
        "verdict_count": len(repo.list_historical_verdicts()),
    }


@router.get("/verdicts")
def list_verdicts(repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return repo.list_historical_verdicts()


@router.get("/verdicts/{verdict_id}")
def get_verdict(verdict_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return repo.get_historical_verdict(verdict_id) or {"error": "not_found"}


@router.get("/daily-review")
def daily_review() -> dict:
    return {
        "processed_sessions": [],
        "new_real_prints": [],
        "service_sessions": [],
        "high_severity_anomalies": [],
        "missing_operator_context": [],
        "new_quality_outcomes": [],
        "reanalyzed_sessions": [],
        "recommended_follow_up_questions": [],
        "failures_or_skipped_items": [],
    }
