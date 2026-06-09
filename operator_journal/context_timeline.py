from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from domain.enums.common import VerificationStatus
from operator_journal.parser import OperatorEventDraft


class ContextUpdateResult(BaseModel):
    context: dict[str, Any]
    changed_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    audit_record: dict[str, Any] = Field(default_factory=dict)


CONTEXT_EVENT_MAP = {
    "material_selected": ["material"],
    "material_changed": ["material"],
    "powder_batch_changed": ["material", "powder_batch"],
    "powder_reused": ["material", "powder_batch", "powder_reuse_count"],
    "gas_cylinder_replaced": ["gas_type", "gas_cylinder_id", "gas_pressure"],
    "gas_type_changed": ["gas_type"],
    "filter_replaced": ["filter_state"],
    "seal_replaced": ["seal_state"],
    "optics_cleaned": ["optics_cleaning_state"],
    "recoater_adjusted": ["recoater_state"],
    "chamber_cleaned": ["chamber_cleaning_state"],
    "calibration_performed": ["calibration_state"],
    "software_update": ["software_version"],
    "configuration_changed": ["configuration_version"],
    "recipe_changed": ["recipe"],
}


def apply_confirmed_operator_event(
    current_context: dict[str, Any],
    event: OperatorEventDraft,
    actor: str,
    at: datetime,
) -> ContextUpdateResult:
    if event.verification_status not in {
        VerificationStatus.operator_confirmed,
        VerificationStatus.supervisor_confirmed,
        VerificationStatus.system_matched,
    }:
        return ContextUpdateResult(
            context=current_context.copy(),
            audit_record={"action": "ignored_unconfirmed_event", "event_type": event.event_type},
        )
    next_context = current_context.copy()
    changed: list[str] = []
    conflicts: list[str] = []

    def set_field(name: str, value: Any) -> None:
        if value in (None, ""):
            return
        if name in next_context and next_context[name] not in (None, value):
            conflicts.append(name)
        next_context[name] = value
        changed.append(name)

    if event.material:
        set_field("material", event.material)
    if event.powder_batch:
        set_field("powder_batch", event.powder_batch)
    if event.gas_type:
        set_field("gas_type", event.gas_type)
    if event.gas_cylinder_id:
        set_field("gas_cylinder_id", event.gas_cylinder_id)
    if event.value and event.unit == "reuse_cycle":
        set_field("powder_reuse_count", int(float(event.value)))
    if event.event_type in {"filter_replaced", "seal_replaced", "optics_cleaned", "recoater_adjusted", "chamber_cleaned", "calibration_performed"}:
        field = CONTEXT_EVENT_MAP[event.event_type][0]
        set_field(field, {"state": event.event_type, "updated_at": at.isoformat()})
    if event.event_type == "gas_cylinder_replaced" and event.value:
        set_field("gas_pressure", {"value": event.value, "unit": event.unit})

    return ContextUpdateResult(
        context=next_context,
        changed_fields=sorted(set(changed)),
        conflicts=sorted(set(conflicts)),
        audit_record={
            "action": "apply_confirmed_operator_event",
            "actor": actor,
            "timestamp": at.isoformat(),
            "event_type": event.event_type,
            "changed_fields": sorted(set(changed)),
            "conflicts": sorted(set(conflicts)),
        },
    )


def calculate_context_features(context: dict[str, Any], session_start: datetime | None = None) -> dict[str, Any]:
    features = {
        "material": context.get("material"),
        "powder_batch": context.get("powder_batch"),
        "powder_reuse_count": context.get("powder_reuse_count"),
        "gas_cylinder_id": context.get("gas_cylinder_id"),
        "gas_type": context.get("gas_type"),
    }
    for key in ("filter_state", "seal_state", "optics_cleaning_state", "recoater_state", "chamber_cleaning_state", "calibration_state"):
        state = context.get(key)
        if isinstance(state, dict) and session_start and state.get("updated_at"):
            try:
                updated = datetime.fromisoformat(state["updated_at"])
                # Normalize both to UTC-aware so a stored naive 'updated_at' vs an
                # aware session_start (or vice versa) can't raise TypeError on
                # subtraction (which 'except ValueError' would NOT catch).
                start = session_start if session_start.tzinfo else session_start.replace(tzinfo=timezone.utc)
                upd = updated if updated.tzinfo else updated.replace(tzinfo=timezone.utc)
                features[f"{key}_age_hours"] = (start - upd).total_seconds() / 3600
            except (ValueError, TypeError):
                features[f"{key}_age_hours"] = None
    return features

