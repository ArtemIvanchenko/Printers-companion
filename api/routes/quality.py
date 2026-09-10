from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from domain.services.quality import create_quality_outcome
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/quality-outcomes", tags=["quality"])


def _enqueue_model_update(repo: RuntimeRepository, outcome: dict) -> dict | None:
    if not outcome.get("is_final"):
        return None
    session_id = outcome.get("session_id")
    if not session_id:
        return None
    from analytics.prediction.retraining import enqueue_retraining

    return enqueue_retraining(
        repo.db,
        session_id=str(session_id),
        outcome_id=str(outcome["outcome_id"]),
        result=str(outcome.get("result") or ""),
        timestamp=str(outcome.get("timestamp") or ""),
    )


@router.post("")
def create_outcome(payload: dict, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    outcome = create_quality_outcome(payload).model_dump(mode="json")
    repo.create_quality_outcome(outcome)
    job = _enqueue_model_update(repo, outcome)
    repo.flush()
    return {**outcome, "model_retraining_job_id": (job or {}).get("job_id")}


@router.get("")
def list_outcomes(
    session_id: str | None = None,
    print_record_id: str | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> list[dict]:
    """List inspection outcomes, optionally scoped to a session or print card."""
    return repo.list_quality_outcomes(
        session_id=session_id,
        print_record_id=print_record_id,
    )


@router.get("/{outcome_id}")
def get_outcome(outcome_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    outcome = repo.get_quality_outcome(outcome_id)
    if not outcome:
        raise HTTPException(status_code=404, detail="Quality outcome not found")
    return outcome


@router.post("/{outcome_id}/link-session")
def link_outcome_session(
    outcome_id: str,
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    outcome = get_outcome(outcome_id, repo)
    if outcome.get("is_final"):
        raise HTTPException(
            status_code=409,
            detail="Подтверждённый итог следует за карточкой печати и не перепривязывается вручную",
        )
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    outcome = repo.link_quality_outcome_session(outcome_id, str(session_id))
    job = _enqueue_model_update(repo, outcome)
    repo.flush()
    return {**outcome, "model_retraining_job_id": (job or {}).get("job_id")}
