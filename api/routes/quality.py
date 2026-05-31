from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from domain.services.quality import create_quality_outcome
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/quality-outcomes", tags=["quality"])


@router.post("")
def create_outcome(payload: dict, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    outcome = create_quality_outcome(payload).model_dump(mode="json")
    repo.save_quality_outcome(outcome)
    repo.commit()
    return outcome


@router.get("")
def list_outcomes(repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return repo.list_quality_outcomes()


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
    outcome["session_id"] = payload["session_id"]
    repo.save_quality_outcome(outcome)
    repo.commit()
    return outcome
