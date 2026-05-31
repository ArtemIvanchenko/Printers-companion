from fastapi import APIRouter

from domain.enums.common import SourceChannel
from operator_journal.parser import OperatorEventDraft, parse_operator_text


router = APIRouter(prefix="/operator-events", tags=["operator-events"])


@router.post("/draft")
def create_operator_event_draft(payload: dict) -> OperatorEventDraft:
    message = payload.get("message") or payload.get("note") or ""
    channel = SourceChannel(payload.get("source_channel", SourceChannel.api.value))
    return parse_operator_text(message, source_channel=channel)

