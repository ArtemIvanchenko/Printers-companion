from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/insights", tags=["insights"])


@router.get("")
def list_insights(repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return repo.list_insights()


@router.get("/{insight_id}")
def get_insight(insight_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return _get(insight_id, repo)


@router.post("/{insight_id}/confirm")
def confirm_insight(
    insight_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    insight = _get(insight_id, repo)
    insight["status"] = "confirmed"
    insight.setdefault("audit_trail", []).append({"action": "confirm", "payload": payload or {}})
    repo.save_insight(insight)
    repo.commit()
    return insight


@router.post("/{insight_id}/dismiss")
def dismiss_insight(
    insight_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    insight = _get(insight_id, repo)
    insight["status"] = "dismissed"
    insight.setdefault("audit_trail", []).append({"action": "dismiss", "payload": payload or {}})
    repo.save_insight(insight)
    repo.commit()
    return insight


@router.post("/{insight_id}/annotate")
def annotate_insight(
    insight_id: str,
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    insight = _get(insight_id, repo)
    insight.setdefault("audit_trail", []).append({"action": "annotate", "payload": payload})
    repo.save_insight(insight)
    repo.commit()
    return insight


@router.post("/{insight_id}/mark-monitoring")
def mark_monitoring(insight_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    insight = _get(insight_id, repo)
    insight["status"] = "monitoring"
    repo.save_insight(insight)
    repo.commit()
    return insight


def _get(insight_id: str, repo: RuntimeRepository) -> dict:
    insight = repo.get_insight(insight_id)
    if not insight:
        raise HTTPException(status_code=404, detail="Insight not found")
    return insight
