"""Operator-visible state of the workstation-local NAS transport queue."""

from fastapi import APIRouter, HTTPException

from core.config.settings import get_settings
from storage.sync.local_outbox import LocalNasOutbox

router = APIRouter(prefix="/sync", tags=["sync"])


def _outbox() -> LocalNasOutbox:
    return LocalNasOutbox.from_settings(get_settings())


@router.get("/status")
def sync_status() -> dict:
    """Queue health without contacting PostgreSQL, MinIO or Redis."""
    settings = get_settings()
    return {
        "compute_node_id": settings.compute_node_id,
        "nas_does_compute": False,
        "outbox": _outbox().status(),
    }


@router.get("/operations/{operation_id}")
def sync_operation(operation_id: str) -> dict:
    operation = _outbox().get(operation_id)
    if operation is None:
        raise HTTPException(404, "Операция синхронизации не найдена")
    return operation


@router.post("/operations/{operation_id}/retry")
def retry_sync_operation(operation_id: str) -> dict:
    outbox = _outbox()
    if not outbox.retry_failed(operation_id):
        operation = outbox.get(operation_id)
        if operation is None:
            raise HTTPException(404, "Операция синхронизации не найдена")
        raise HTTPException(409, "Повторить можно только операцию со статусом failed")
    return outbox.get(operation_id) or {"operation_id": operation_id, "status": "pending"}
