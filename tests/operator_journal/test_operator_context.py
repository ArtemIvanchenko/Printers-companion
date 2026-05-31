from datetime import datetime, timezone

from domain.enums.common import VerificationStatus
from operator_journal.context_timeline import apply_confirmed_operator_event
from operator_journal.parser import parse_operator_text


def test_russian_gas_message_becomes_structured_draft() -> None:
    draft = parse_operator_text("Поставили новый баллон аргона, баллон AG-042, давление 180 бар")
    assert draft.event_type == "gas_cylinder_replaced"
    assert draft.gas_cylinder_id == "AG-042"
    assert draft.value == "180"


def test_confirmed_event_updates_context_with_conflict_audit() -> None:
    draft = parse_operator_text("Порошок 316L партия S-17 reuse cycle 3")
    draft.verification_status = VerificationStatus.operator_confirmed
    result = apply_confirmed_operator_event(
        {"powder_batch": "OLD"},
        draft,
        actor="tester",
        at=datetime(2026, 4, 27, tzinfo=timezone.utc),
    )
    assert result.context["powder_batch"] == "S-17"
    assert "powder_batch" in result.conflicts
    assert result.context["powder_reuse_count"] == 3

