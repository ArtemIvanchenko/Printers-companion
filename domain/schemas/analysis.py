"""Versioned read contract for measured session facts (no I/O)."""
from typing import Any


def measured_group(group: dict[str, Any]) -> dict[str, Any]:
    """Prefer published facts; retain legacy display-only extensions.

    Absence is a legacy payload. A present malformed/future snapshot is not
    permission to silently substitute potentially stale display fields.
    """
    snapshot = group.get("analysis_snapshot")
    if snapshot is None:
        return group
    if (not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1
            or not snapshot.get("analysis_id")
            or any(not isinstance(snapshot.get(key), dict)
                   for key in ("features", "health", "data_quality", "signal_stats"))):
        raise ValueError("Неподдерживаемый или повреждённый аналитический снимок")
    return {**group, **{key: snapshot[key] for key in (
        "features", "health", "data_quality", "signal_stats", "classification",
        "confidence", "evidence", "telemetry_evidence",
    ) if key in snapshot}}
