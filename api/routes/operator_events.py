from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException

from api.deps.repositories import get_runtime_repository
from domain.enums.common import SourceChannel, VerificationStatus
from operator_journal.parser import OperatorEventDraft, parse_operator_text
from storage.repositories.runtime import RuntimeRepository


router = APIRouter(prefix="/operator-events", tags=["operator-events"])


@router.post("")
def create_operator_event(payload: dict, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    event_id = payload.get("event_id") or f"op_event_{uuid4().hex}"
    event = payload | {
        "event_id": event_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "verification_status": payload.get("verification_status", "unverified"),
    }
    repo.save_operator_event(event)
    repo.flush()
    return event


@router.post("/draft")
def create_operator_event_draft(payload: dict) -> OperatorEventDraft:
    return parse_operator_text(
        payload.get("message") or payload.get("note") or "",
        source_channel=SourceChannel(payload.get("source_channel", SourceChannel.api.value)),
    )


@router.post("/{event_id}/confirm")
def confirm_operator_event(
    event_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    event = _get(event_id, repo)
    event["verification_status"] = VerificationStatus.operator_confirmed.value
    event.setdefault("audit_trail", []).append({"action": "confirm", "payload": payload or {}})
    repo.save_operator_event(event)
    repo.flush()
    return event


@router.post("/{event_id}/dismiss")
def dismiss_operator_event(
    event_id: str,
    payload: dict | None = None,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    event = _get(event_id, repo)
    event["verification_status"] = VerificationStatus.dismissed.value
    event.setdefault("audit_trail", []).append({"action": "dismiss", "payload": payload or {}})
    repo.save_operator_event(event)
    repo.flush()
    return event


@router.patch("/{event_id}")
def update_operator_event(
    event_id: str,
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    event = _get(event_id, repo)
    before = event.copy()
    event.update(payload)
    event.setdefault("audit_trail", []).append({"action": "patch", "before": before, "after": event.copy()})
    repo.save_operator_event(event)
    repo.flush()
    return event


@router.get("")
def list_operator_events(repo: RuntimeRepository = Depends(get_runtime_repository)) -> list[dict]:
    return repo.list_operator_events()


@router.get("/{event_id}")
def get_operator_event(event_id: str, repo: RuntimeRepository = Depends(get_runtime_repository)) -> dict:
    return _get(event_id, repo)


@router.post("/{event_id}/link-session")
def link_operator_event_session(
    event_id: str,
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    event = _get(event_id, repo)
    session_id = payload.get("session_id")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
    event["session_id"] = session_id
    repo.save_operator_event(event)
    repo.flush()
    return event


@router.post("/{event_id}/link-anomaly")
def link_operator_event_anomaly(
    event_id: str,
    payload: dict,
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    event = _get(event_id, repo)
    anomaly_id = payload.get("anomaly_id")
    if not anomaly_id:
        raise HTTPException(status_code=400, detail="anomaly_id is required")
    event.setdefault("linked_machine_events", []).append(anomaly_id)
    repo.save_operator_event(event)
    repo.flush()
    return event


def _get(event_id: str, repo: RuntimeRepository) -> dict:
    event = repo.get_operator_event(event_id)
    if not event:
        raise HTTPException(status_code=404, detail="Operator event not found")
    return event
