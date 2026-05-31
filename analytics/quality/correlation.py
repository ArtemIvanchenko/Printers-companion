from typing import Any


def summarize_quality_context(quality_outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(quality_outcomes)
    rejected = sum(1 for outcome in quality_outcomes if outcome.get("result") == "rejected")
    warnings = sum(1 for outcome in quality_outcomes if outcome.get("result") == "warning")
    defect_counts: dict[str, int] = {}
    for outcome in quality_outcomes:
        defect = outcome.get("defect_type") or "none"
        defect_counts[defect] = defect_counts.get(defect, 0) + 1
    return {
        "quality_outcome_count": total,
        "rejected_count": rejected,
        "warning_count": warnings,
        "defect_counts": defect_counts,
        "coverage": 1.0 if total else 0.0,
    }

