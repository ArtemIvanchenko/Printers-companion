from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from domain.schemas.parsing import CanonicalEventDraft, StateTransitionDraft


def _aware(dt: datetime) -> datetime:
    """Force a datetime to UTC-aware so naive/aware values never mix in min/max
    (mixing raises TypeError)."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def extract_layer_features(events: list[CanonicalEventDraft]) -> list[dict[str, Any]]:
    by_layer: dict[int, list[CanonicalEventDraft]] = defaultdict(list)
    for event in events:
        if event.layer is not None and event.ts is not None:
            by_layer[event.layer].append(event)
    features: list[dict[str, Any]] = []
    previous_duration: float | None = None
    for layer in sorted(by_layer):
        times = sorted(_aware(event.ts) for event in by_layer[layer] if event.ts)
        duration = (times[-1] - times[0]).total_seconds() if len(times) >= 2 else None
        delta = None if previous_duration is None or duration is None else duration - previous_duration
        features.append(
            {
                "layer": layer,
                "event_count": len(by_layer[layer]),
                "layer_duration_sec": duration,
                "delta_from_previous_layer_sec": delta,
                "event_types": sorted({event.event_type for event in by_layer[layer]}),
            }
        )
        if duration is not None:
            previous_duration = duration
    return features


def extract_session_features(
    events: list[CanonicalEventDraft],
    transitions: list[StateTransitionDraft],
    production_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_counts = Counter(event.event_type for event in events)
    subsystem_counts = Counter(transition.subsystem or "unknown" for transition in transitions)
    timestamps = (
        [_aware(event.ts) for event in events if event.ts]
        + [_aware(transition.ts_start) for transition in transitions if transition.ts_start]
    )
    start = min(timestamps) if timestamps else None
    end = max(timestamps) if timestamps else None
    duration = (end - start).total_seconds() if start and end else None
    return {
        "completion_status": "finished" if event_counts.get("finish") else "unknown",
        "pause_count": event_counts.get("pause", 0),
        "restart_attempt_count": event_counts.get("restart_attempt", 0) + event_counts.get("resume", 0),
        "service_state_density": subsystem_counts.get("door", 0) + subsystem_counts.get("glove", 0),
        "subsystem_instability": dict(subsystem_counts),
        "duration_sec": duration,
        "material": (production_context or {}).get("material"),
        "powder_batch": (production_context or {}).get("powder_batch"),
        "gas_cylinder_id": (production_context or {}).get("gas_cylinder_id"),
    }

