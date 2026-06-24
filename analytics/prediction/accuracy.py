"""Predicted-vs-actual print time: accuracy report and auto-calibration.

A prediction snapshot is stored on the PrintRecord (``metadata_json["prediction"]``)
when the operator runs the estimate for the record's STLs. It carries the
**raw** (uncorrected) geometric estimate ``raw_print_hours`` and the material.
Once the record is linked to a log session the actual duration is known and the
pair feeds calibration.

Calibration is per material: the factor for a material is the median of
``actual / raw_predicted`` over its pairs. Calibrating against the *raw* estimate
keeps the factor absolute, so it never compounds on a previously-corrected value.

``recalibrate_and_apply`` writes the learned factors into
``machine_params.time_correction_by_mat`` automatically (unless the operator has
pinned them with ``correction_locked``), within sanity bounds — out-of-range
ratios signal a parameter/orientation problem, not a calibration one.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.prints import MachineParams, PrintRecord
from domain.models.sessions import BuildSession

logger = logging.getLogger(__name__)

MIN_PAIRS_FOR_CALIBRATION = 3
# Use only the most recent N pairs per material, so calibration tracks the
# machine's current state instead of dragging in stale history forever.
CALIBRATION_WINDOW = 20
# A learned factor outside this range almost always means wrong machine
# parameters / orientation rather than a real systematic offset — don't apply
# it silently; surface it instead.
CORRECTION_MIN, CORRECTION_MAX = 0.5, 2.0


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _actual_hours(session: BuildSession) -> float | None:
    if not session.start_ts or not session.end_ts:
        return None
    hours = (_as_utc(session.end_ts) - _as_utc(session.start_ts)).total_seconds() / 3600.0
    return hours if hours > 0 else None


def _raw_predicted(snapshot: dict) -> float | None:
    """Uncorrected geometric hours for this snapshot (with legacy fallbacks)."""
    raw = snapshot.get("raw_print_hours")
    if raw is None:
        raw = snapshot.get("print_hours")
    if raw is None:  # legacy two-method snapshots: prefer the accurate one
        raw = (snapshot.get("accurate") or {}).get("print_hours")
    return raw if (raw and raw > 0) else None


def prediction_accuracy(db: Session) -> dict:
    """Compare stored prediction snapshots with actual session durations.

    Returns per-pair rows and per-material suggested factors plus an overall
    suggested factor (median across all pairs) for display.
    """
    records = db.scalars(
        select(PrintRecord).where(PrintRecord.session_id.is_not(None))
    ).all()

    rows: list[dict] = []
    ratios_by_mat: dict[str, list[float]] = defaultdict(list)
    all_ratios: list[float] = []
    for record in records:
        snapshot = (record.metadata_json or {}).get("prediction")
        if not snapshot:
            continue
        session = db.get(BuildSession, record.session_id)
        actual = _actual_hours(session) if session else None
        raw = _raw_predicted(snapshot)
        if actual is None or raw is None:
            continue

        material = (snapshot.get("material") or record.material or "—")
        ratio = actual / raw
        ratios_by_mat[material].append(ratio)
        all_ratios.append(ratio)
        rows.append({
            "record_id": record.record_id,
            "name": record.name,
            "session_id": record.session_id,
            "material": material,
            "actual_hours": round(actual, 2),
            "predicted_hours": round(raw, 2),
            "error_pct": round((raw - actual) / actual * 100, 1),
            "estimated_at": snapshot.get("estimated_at"),
        })

    def _median(ratios: list[float]) -> float | None:
        # Newest pairs first preserved by record order is not guaranteed, so
        # just window by count — median is order-independent anyway.
        sample = ratios[-CALIBRATION_WINDOW:]
        if len(sample) < MIN_PAIRS_FOR_CALIBRATION:
            return None
        return round(statistics.median(sample), 3)

    by_material = {
        mat: {"n_pairs": len(r), "suggested_factor": _median(r)}
        for mat, r in ratios_by_mat.items()
    }

    return {
        "pairs": rows,
        "n_pairs": len(rows),
        "by_material": by_material,
        # Overall median across materials — for the headline display only.
        "suggested_correction_factor": _median(all_ratios),
        "min_pairs_for_calibration": MIN_PAIRS_FOR_CALIBRATION,
    }


def recalibrate_and_apply(db: Session) -> dict:
    """Recompute per-material factors from history and persist the in-range ones.

    No-op when the operator has pinned factors (``correction_locked``). Returns a
    summary {applied: {...}, skipped: [...], locked: bool}. Caller's unit of work
    commits — this only mutates the row.
    """
    report = prediction_accuracy(db)
    by_material = report["by_material"]

    row = db.get(MachineParams, 1)
    if row is None:
        return {"applied": {}, "skipped": [], "locked": False, "reason": "no machine params"}
    if row.correction_locked:
        return {"applied": {}, "skipped": [], "locked": True}

    current = dict(row.time_correction_by_mat or {})
    applied: dict[str, float] = {}
    skipped: list[dict] = []
    for material, info in by_material.items():
        factor = info["suggested_factor"]
        if factor is None:
            continue  # not enough pairs yet
        if not (CORRECTION_MIN <= factor <= CORRECTION_MAX):
            skipped.append({"material": material, "factor": factor, "reason": "out_of_range"})
            logger.warning(
                "calibration: %s factor %.3f out of [%.1f, %.1f] — not applied "
                "(check machine params / orientation)",
                material, factor, CORRECTION_MIN, CORRECTION_MAX,
            )
            continue
        if current.get(material) != factor:
            logger.info("calibration: %s ×%s → ×%.3f (%d pairs)",
                        material, current.get(material), factor, info["n_pairs"])
            applied[material] = factor

    if applied:
        current.update(applied)
        row.time_correction_by_mat = current
        row.updated_at = datetime.now(timezone.utc)

    return {"applied": applied, "skipped": skipped, "locked": False}


__all__ = [
    "prediction_accuracy",
    "recalibrate_and_apply",
    "MIN_PAIRS_FOR_CALIBRATION",
    "CORRECTION_MIN",
    "CORRECTION_MAX",
]
