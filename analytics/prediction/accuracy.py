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
from collections.abc import Iterator
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

# Percentile band for the print-time uncertainty interval, taken from the same
# actual/raw ratio history the point factor uses. 0.1/0.9 (an 80% band) rather
# than a tighter one — with only MIN_PAIRS_FOR_CALIBRATION..CALIBRATION_WINDOW
# points, a 95% band's tails are single outlier pairs and swing wildly print
# to print; 80% is still informative without over-claiming precision on a
# handful of samples.
RATIO_INTERVAL_LOW, RATIO_INTERVAL_HIGH = 0.1, 0.9

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


# Machine-time actuals are only trusted when the time_log covers (almost) the
# whole print: a partial log (multi-day rotation, truncated file) sums LESS
# machine time than the print really took and would drag the factor down.
_MACHINE_TIME_MIN_COVERAGE = 0.95
# Without a known expected layer count we cannot check coverage at all — a
# missing/legacy snapshot must not let a two-layer partial log pass as "the
# whole print". Require this many logged layers as a weak floor in that case;
# it does not replace the coverage check, only covers its absence.
_MACHINE_TIME_MIN_LAYERS_NO_EXPECTED = 100


def _machine_hours_from_logs(
    session_id: str, expected_layers: int | None, db: Session,
) -> float | None:
    """Pause-free machine hours: Σ(burn_ms + pour_ms) over the session's time_log.

    This is the project's calibration principle (см. базу знаний в
    plate_estimator.py, п.1): predictions model machine time only, so actuals
    must be machine time too. The wall-clock session span includes operator
    pauses — on a real build 18 of 47.6 hours — and calibrating against it
    bakes pauses into every quoted time.

    Returns None when the time_log is absent or does not plausibly cover the
    whole print (see the two floors above — ``expected_layers`` is normally
    present, ``_MACHINE_TIME_MIN_LAYERS_NO_EXPECTED`` only guards its absence).
    """
    from analytics.prediction.recoat_calibration import session_machine_seconds_by_layer

    per_layer = session_machine_seconds_by_layer(session_id, db)
    if not per_layer:
        return None
    if expected_layers:
        if len(per_layer) < _MACHINE_TIME_MIN_COVERAGE * expected_layers:
            return None
    elif len(per_layer) < _MACHINE_TIME_MIN_LAYERS_NO_EXPECTED:
        return None
    return sum(per_layer.values()) / 3600.0


def iter_linked_prints(db: Session) -> "Iterator[tuple[PrintRecord, BuildSession]]":
    """Every print card that has a log session, with the session attached.

    All three calibrations start the same way — select the linked records,
    batch-load their sessions (never one ``db.get`` per row), then walk the
    pairs. Only what they extract from each pair differs. Sharing the walk
    keeps the "which rows are even candidates" question answered in one place:
    the same filter was silently dropped from ``analysis._load_sessions``,
    which then averaged preparation runs into the maintenance forecast.

    Records whose session id points at nothing are skipped — that is a dangling
    link, not a calibration input.
    """
    records = db.scalars(
        select(PrintRecord).where(PrintRecord.session_id.is_not(None))
    ).all()
    session_ids = [r.session_id for r in records if r.session_id]
    if not session_ids:
        return

    sessions = {
        s.session_id: s
        for s in db.scalars(
            select(BuildSession).where(BuildSession.session_id.in_(session_ids))
        ).all()
    }
    for record in records:
        session = sessions.get(record.session_id)
        if session is not None:
            yield record, session


def printed_at(record: PrintRecord, session: BuildSession) -> datetime:
    """When this print actually ran — the log wins, the card is the fallback.

    Calibration windows are "the most recent N", so this is what makes recency
    mean anything.
    """
    return as_utc(session.start_ts) if session.start_ts else as_utc(record.created_at)


def _actual_hours(session: BuildSession) -> float | None:
    """Wall-clock print span in hours — the FALLBACK actual, pause-contaminated.

    ``start_ts``/``end_ts`` are the monitor100-excluded print span computed by
    ``compute_print_span``. Callers must prefer ``_machine_hours_from_logs``;
    this remains only for sessions whose time_log is missing or incomplete.
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


def _quantile_bounds(sample: list[float], low: float, high: float) -> tuple[float, float]:
    """Linear-interpolated [low, high] percentile bounds of ``sample``.

    Same interpolation convention as ``numpy.percentile`` (reimplemented here
    rather than pulling in numpy just for this one call).
    """
    s = sorted(sample)

    def _pct(p: float) -> float:
        idx = p * (len(s) - 1)
        lo = int(idx)
        frac = idx - lo
        return s[lo] + frac * (s[lo + 1] - s[lo]) if lo + 1 < len(s) else s[lo]

    return round(_pct(low), 3), round(_pct(high), 3)


def prediction_accuracy(db: Session) -> dict:
    """Compare stored prediction snapshots with actual session durations.

    Returns per-pair rows and per-material suggested factors plus an overall
    suggested factor (median across all pairs) for display.
    """
    rows: list[dict] = []
    # (sort key, ratio) so the calibration window can be taken by recency.
    usable_by_mat: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    all_usable: list[tuple[datetime, float]] = []
    excluded: list[dict] = []

    for record, session in iter_linked_prints(db):
        snapshot = (record.metadata_json or {}).get("prediction")
        if not snapshot:
            continue
        # Pause-free machine time from the printer's own logs is the ONLY
        # actual consistent with what the model predicts; the wall-clock
        # span is a legacy fallback and carries operator pauses.
        actual = _machine_hours_from_logs(
            record.session_id, snapshot.get("layer_count"), db,
        )
        actual_source = "machine_log" if actual is not None else None
        if actual is None:
            actual = _actual_hours(session)
            actual_source = "wall_span" if actual is not None else None
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
        when = printed_at(record, session)
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
            "actual_source": actual_source,
            "printed_at": when.isoformat(),
            "estimated_at": snapshot.get("estimated_at"),
        })

    rows.sort(key=lambda r: r["printed_at"], reverse=True)

    def _window_sample(pairs: list[tuple[datetime, float]]) -> list[float]:
        return [ratio for _, ratio in sorted(pairs, key=lambda p: p[0], reverse=True)[:CALIBRATION_WINDOW]]

    def _median(pairs: list[tuple[datetime, float]]) -> float | None:
        """Median ratio over the most recent CALIBRATION_WINDOW pairs.

        Sorting by print date is what makes the window mean "recent"; the old
        code sliced the list in DB-scan order, so it kept an arbitrary subset
        while claiming to track the machine's current state.
        """
        sample = _window_sample(pairs)
        if len(sample) < MIN_PAIRS_FOR_CALIBRATION:
            return None
        return round(statistics.median(sample), 3)

    def _ratio_interval(pairs: list[tuple[datetime, float]]) -> tuple[float, float] | None:
        """[RATIO_INTERVAL_LOW, RATIO_INTERVAL_HIGH] percentile band of actual/raw,
        over the same recency window as the point factor. ``None`` — not a
        fabricated spread — below MIN_PAIRS_FOR_CALIBRATION.
        """
        sample = _window_sample(pairs)
        if len(sample) < MIN_PAIRS_FOR_CALIBRATION:
            return None
        return _quantile_bounds(sample, RATIO_INTERVAL_LOW, RATIO_INTERVAL_HIGH)

    by_material = {
        mat: {
            "n_pairs": len(pairs),
            "suggested_factor": _median(pairs),
            "ratio_interval": _ratio_interval(pairs),
        }
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


def calibration_interval_hours(
    db: Session, material: str, raw_hours: float,
) -> tuple[float, float] | None:
    """Print-time interval in hours for ``material``, from calibration history.

    Scales the material's actual/raw ratio interval (``_ratio_interval`` inside
    ``prediction_accuracy``) by ``raw_hours`` — the same raw geometric estimate
    the ratio was computed against, so this must be called with the *raw*
    (uncorrected) hours, not the already-corrected quote.

    Returns ``None`` — never a fabricated interval — when the material has
    fewer than ``MIN_PAIRS_FOR_CALIBRATION`` usable pairs, exactly like the
    point correction factor.
    """
    report = prediction_accuracy(db)
    info = report["by_material"].get(material)
    if not info or info["ratio_interval"] is None:
        return None
    low, high = info["ratio_interval"]
    return round(low * raw_hours, 3), round(high * raw_hours, 3)


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
    "calibration_interval_hours",
    "MIN_PAIRS_FOR_CALIBRATION",
    "CALIBRATION_WINDOW",
    "CORRECTION_MIN",
    "CORRECTION_MAX",
    "RATIO_INTERVAL_LOW",
    "RATIO_INTERVAL_HIGH",
    "PRINT_CLASSIFICATIONS",
    "as_utc",
    "session_classification",
]
