from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from domain.schemas.parsing import CanonicalEventDraft, StateTransitionDraft


class SegmentDraft(BaseModel):
    segment_id: str = Field(default_factory=lambda: f"segment_{uuid4().hex}")
    phase: str
    ts_start: datetime | None = None
    ts_end: datetime | None = None
    layer_start: int | None = None
    layer_end: int | None = None
    confidence: float = 0.6
    evidence: list[dict[str, Any]] = Field(default_factory=list)


def segment_phases(
    events: list[CanonicalEventDraft],
    transitions: list[StateTransitionDraft],
    configured_phases: dict[str, Any] | None = None,
) -> list[SegmentDraft]:
    segments: list[SegmentDraft] = []
    sorted_events = sorted([event for event in events if event.ts], key=lambda event: event.ts)
    current: SegmentDraft | None = None
    for event in sorted_events:
        phase = event.phase or _phase_from_event_type(event.event_type)
        if phase is None:
            continue
        if current is None or current.phase != phase:
            if current:
                current.ts_end = event.ts
                segments.append(current)
            current = SegmentDraft(
                phase=phase,
                ts_start=event.ts,
                layer_start=event.layer,
                layer_end=event.layer,
                confidence=event.confidence,
                evidence=[_event_evidence(event)],
            )
        else:
            current.ts_end = event.ts
            current.layer_end = event.layer or current.layer_end
            current.evidence.append(_event_evidence(event))
    if current:
        segments.append(current)

    if not segments and transitions:
        for transition in transitions:
            phase = _phase_from_subsystem(transition.subsystem)
            if phase:
                segments.append(
                    SegmentDraft(
                        phase=phase,
                        ts_start=transition.ts_start,
                        ts_end=transition.ts_end,
                        confidence=0.45,
                        evidence=[
                            {
                                "kind": "state_transition",
                                "subsystem": transition.subsystem,
                                "changed_columns": transition.changed_columns,
                                "source_offset_start": transition.source_offset_start,
                            }
                        ],
                    )
                )
    return segments


def _event_evidence(event: CanonicalEventDraft) -> dict[str, Any]:
    return {
        "kind": "event",
        "event_type": event.event_type,
        "source_file_id": event.source.source_file_id,
        "source_line": event.source.source_line,
        "raw_excerpt": event.source.raw_excerpt,
    }


def _phase_from_event_type(event_type: str) -> str | None:
    if "burn" in event_type:
        return "burn"
    if "pour" in event_type:
        return "pour"
    if event_type in {"pause"}:
        return "pause"
    if event_type in {"resume", "restart_attempt"}:
        return "restart_attempts"
    return None


def _phase_from_subsystem(subsystem: str | None) -> str | None:
    return {
        "heating": "heat_up",
        "chamber": "chamber_prep",
        "glove": "chamber_prep",
        "door": "chamber_prep",
        "parameter_check": "parameter_check",
        "powder": "powder_prep",
        "laser_interlock": "restart_attempts",
    }.get(subsystem or "")

