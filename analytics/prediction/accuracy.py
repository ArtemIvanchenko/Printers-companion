"""Predicted-vs-actual print time: accuracy report and calibration.

A prediction snapshot is stored on the PrintRecord (metadata_json["prediction"])
when the operator runs the estimate for the record's STL. Once the record is
linked to a log session, the actual duration is known and the pair feeds this
report. The suggested ``time_correction_factor`` is the median of
actual/predicted ratios for the physics-based method — the operator applies
it via machine settings (one click in the dashboard).
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession

logger = logging.getLogger(__name__)

MIN_PAIRS_FOR_CALIBRATION = 3


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _actual_hours(session: BuildSession) -> float | None:
    if not session.start_ts or not session.end_ts:
        return None
    hours = (_as_utc(session.end_ts) - _as_utc(session.start_ts)).total_seconds() / 3600.0
    return hours if hours > 0 else None


def prediction_accuracy(db: Session) -> dict:
    """Compare stored prediction snapshots with actual session durations."""
    records = db.scalars(
        select(PrintRecord).where(PrintRecord.session_id.is_not(None))
    ).all()

    rows: list[dict] = []
    physics_ratios: list[float] = []
    excel_ratios: list[float] = []
    for record in records:
        snapshot = (record.metadata_json or {}).get("prediction")
        if not snapshot:
            continue
        session = db.get(BuildSession, record.session_id)
        actual = _actual_hours(session) if session else None
        if actual is None:
            continue

        row = {
            "record_id": record.record_id,
            "name": record.name,
            "session_id": record.session_id,
            "actual_hours": round(actual, 2),
            "estimated_at": snapshot.get("estimated_at"),
        }
        for method_key, ratios in (("fast", excel_ratios), ("accurate", physics_ratios)):
            predicted = (snapshot.get(method_key) or {}).get("print_hours")
            if predicted and predicted > 0:
                row[f"{method_key}_hours"] = round(predicted, 2)
                row[f"{method_key}_error_pct"] = round((predicted - actual) / actual * 100, 1)
                ratios.append(actual / predicted)
        rows.append(row)

    def _suggest(ratios: list[float]) -> float | None:
        if len(ratios) < MIN_PAIRS_FOR_CALIBRATION:
            return None
        return round(statistics.median(ratios), 3)

    return {
        "pairs": rows,
        "n_pairs": len(rows),
        # Median actual/predicted: multiply the physics estimate by this to match reality
        "suggested_correction_factor": _suggest(physics_ratios),
        "excel_median_ratio": _suggest(excel_ratios),
        "min_pairs_for_calibration": MIN_PAIRS_FOR_CALIBRATION,
    }


__all__ = ["prediction_accuracy", "MIN_PAIRS_FOR_CALIBRATION"]
