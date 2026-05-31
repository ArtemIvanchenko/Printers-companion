from datetime import datetime, timezone

from domain.enums.common import SourceChannel, VerificationStatus
from operator_journal.context_timeline import ContextUpdateResult, apply_confirmed_operator_event
from operator_journal.parser import OperatorEventDraft, parse_operator_text


def create_draft_from_message(message: str, source_channel: SourceChannel = SourceChannel.telegram) -> OperatorEventDraft:
    return parse_operator_text(message, source_channel=source_channel)


def confirm_event_and_update_context(
    context: dict,
    draft: OperatorEventDraft,
    actor: str,
    supervisor: bool = False,
) -> ContextUpdateResult:
    draft.verification_status = (
        VerificationStatus.supervisor_confirmed if supervisor else VerificationStatus.operator_confirmed
    )
    return apply_confirmed_operator_event(context, draft, actor=actor, at=datetime.now(timezone.utc))

