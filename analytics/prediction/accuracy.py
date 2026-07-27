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

# Sessions the calibration loop refuses to learn from. A session's measured
# span is only a print duration if the session really is a print: service runs,
# idle diagnostics and mis-grouped sessions have spans that are not comparable
# with a geometric estimate, and feeding them in skews the factor that scales
# every quoted time and price.
PRINT_CLASSIFICATIONS = {"REAL_PRINT", "REAL_PRINT_WITH_RESUME"}
# Hard sanity bounds on a measured print span (hours). Outside these the pair is
# reported but never used for calibration.
_MIN_ACTUAL_HOURS, _MAX_ACTUAL_HOURS = 0.25, 24 * 14


def as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def session_classification(session: BuildSession) -> str:
    group = ((session.context or {}).get("runtime_payload", {}) or {}).get("group", {}) or {}
    return group.get("classification") or session.classification or ""


def _actual_hours(session: BuildSession) -> float | None:
    """Measured print span in hours, or None when it cannot be trusted.

    ``start_ts``/``end_ts`` are the monitor100-excluded print span computed by
    ``compute_print_span``; they are only meaningful when the session groups the
    files of exactly one print (see domain.services.session_grouping).
    """
    if not session.start_ts or not session.end_ts:
        return None
    hours = (as_utc(session.end_ts) - as_utc(session.start_ts)).total_seconds() / 3600.0
    return hours if hours > 0 else None


def _usable_for_calibration(session: BuildSession, actual: float) -> str | None:
    """Reason this pair must not train the correction factor, or None if it may."""
    if session_classification(session) not in PRINT_CLASSIFICATIONS:
        return "not_a_print"
    if not (_MIN_ACTUAL_HOURS <= actual <= _MAX_ACTUAL_HOURS):
        return "implausible_duration"
    return None


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

    # Batch-load the linked sessions instead of one db.get() per record.
    session_ids = [r.session_id for r in records if r.session_id]
    sessions: dict[str, BuildSession] = {}
    if session_ids:
        sessions = {
            s.session_id: s
            for s in db.scalars(select(BuildSession).where(BuildSession.session_id.in_(session_ids))).all()
        }

    rows: list[dict] = []
    # (sort key, ratio) so the calibration window can be taken by recency.
    usable_by_mat: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    all_usable: list[tuple[datetime, float]] = []
    excluded: list[dict] = []

    for record in records:
        snapshot = (record.metadata_json or {}).get("prediction")
        if not snapshot:
            continue
        session = sessions.get(record.session_id)
        actual = _actual_hours(session) if session else None
        raw = _raw_predicted(snapshot)
        if actual is None or raw is None:
            continue

        material = (snapshot.get("material") or record.material or "—")
        factor = float(snapshot.get("correction_factor") or 1.0) or 1.0
        # What the operator was actually shown for this record.
        shown = raw * factor
        ratio = actual / raw
        skip_reason = _usable_for_calibration(session, actual)

        # Order pairs by when the print happened, so "most recent N" is real.
        when = as_utc(session.start_ts) if session.start_ts else as_utc(record.created_at)
        if skip_reason is None:
            usable_by_mat[material].append((when, ratio))
            all_usable.append((when, ratio))
        else:
            excluded.append({
                "record_id": record.record_id, "session_id": record.session_id,
                "reason": skip_reason,
            })

        rows.append({
            "record_id": record.record_id,
            "name": record.name,
            "session_id": record.session_id,
            "material": material,
            "actual_hours": round(actual, 2),
            # The corrected figure the operator saw — this is what "error" must
            # be measured against. The raw geometric hours are kept alongside it
            # because that is what the calibration ratio is computed from.
            "predicted_hours": round(shown, 2),
            "raw_predicted_hours": round(raw, 2),
            "correction_factor": round(factor, 3),
            "error_pct": round((shown - actual) / actual * 100, 1),
            "raw_error_pct": round((raw - actual) / actual * 100, 1),
            "used_for_calibration": skip_reason is None,
            "excluded_reason": skip_reason,
            "printed_at": when.isoformat(),
            "estimated_at": snapshot.get("estimated_at"),
        })

    rows.sort(key=lambda r: r["printed_at"], reverse=True)

    def _median(pairs: list[tuple[datetime, float]]) -> float | None:
        """Median ratio over the most recent CALIBRATION_WINDOW pairs.

        Sorting by print date is what makes the window mean "recent"; the old
        code sliced the list in DB-scan order, so it kept an arbitrary subset
        while claiming to track the machine's current state.
        """
        sample = [ratio for _, ratio in sorted(pairs, key=lambda p: p[0], reverse=True)[:CALIBRATION_WINDOW]]
        if len(sample) < MIN_PAIRS_FOR_CALIBRATION:
            return None
        return round(statistics.median(sample), 3)

    by_material = {
        mat: {"n_pairs": len(pairs), "suggested_factor": _median(pairs)}
        for mat, pairs in usable_by_mat.items()
    }

    return {
        "pairs": rows,
        "n_pairs": len(rows),
        "n_usable_pairs": len(all_usable),
        "excluded": excluded,
        "by_material": by_material,
        # Overall median across materials — for the headline display only.
        "suggested_correction_factor": _median(all_usable),
        "min_pairs_for_calibration": MIN_PAIRS_FOR_CALIBRATION,
        "calibration_window": CALIBRATION_WINDOW,
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
    "CALIBRATION_WINDOW",
    "CORRECTION_MIN",
    "CORRECTION_MAX",
    "PRINT_CLASSIFICATIONS",
    "as_utc",
    "session_classification",
]
