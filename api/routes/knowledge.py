from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/knowledge", tags=["knowledge"])


@router.get("")
def list_knowledge(repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return repo.list_knowledge()


@router.post("")
def create_knowledge(payload: dict, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    knowledge_id = payload.get("knowledge_id") or f"knowledge_{uuid4().hex}"
    item = payload | {"knowledge_id": knowledge_id, "status": payload.get("status", "active")}
    repo.save_knowledge(item)
    repo.flush()
    return item


@router.patch("/{knowledge_id}")
def update_knowledge(
    knowledge_id: str,
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    item = _get(knowledge_id, repo)
    item.update(payload)
    repo.save_knowledge(item)
    repo.flush()
    return item


@router.post("/{knowledge_id}/deprecate")
def deprecate_knowledge(knowledge_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    item = _get(knowledge_id, repo)
    item["status"] = "deprecated"
    repo.save_knowledge(item)
    repo.flush()
    return item


def _get(knowledge_id: str, repo: RuntimeRepository) -> dict:
    item = repo.get_knowledge(knowledge_id)
    if not item:
        raise HTTPException(status_code=404, detail="Knowledge not found")
    return item
