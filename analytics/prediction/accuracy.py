"""Predicted-vs-actual print time: accuracy report and auto-calibration.

A prediction snapshot is stored on the PrintRecord (``metadata_json["prediction"]``)
when the operator runs the estimate for the record's STLs. It carries the
**raw** (uncorrected) geometric estimate ``raw_print_hours`` and the material.
Once the record is linked to a log session the actual duration is known and the
pair feeds calibration.

Calibration is per material+layer-thickness mode and applies to **scan only**:
the factor is the median of ``actual_burn / raw_scan`` over trusted pairs.
Recoat has its own calibration from ``pour_ms`` and is never scaled here.

``recalibrate_and_apply`` writes the learned factors into
``machine_params.time_correction_by_mat`` automatically (unless the operator has
pinned them with ``correction_locked``), within sanity bounds — out-of-range
ratios signal a parameter/orientation problem, not a calibration one.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from domain.models.prints import MachineParams, PrintRecord
from domain.models.sessions import BuildSession
from analytics.prediction.layer_engine import scan_model_key

logger = logging.getLogger(__name__)

MIN_PAIRS_FOR_CALIBRATION = 3
# Use only the most recent N pairs per material, so calibration tracks the
# machine's current state instead of dragging in stale history forever.
CALIBRATION_WINDOW = 20
# A learned factor outside this range almost always means wrong machine
# parameters / orientation rather than a real systematic offset — don't apply
# it silently; surface it instead.
CORRECTION_MIN, CORRECTION_MAX = 0.5, 2.0
_CALIBRATION_ADVISORY_LOCK = 0x50524341  # stable PostgreSQL bigint key: "PRCA"


def try_acquire_calibration_lock(db: Session) -> bool:
    """Serialize shared MachineParams calibration across operator PCs.

    The lock is transaction-scoped and PostgreSQL performs no calculation; it
    merely ensures the last finishing workstation cannot overwrite a newer
    calibration from another one. SQLite tests/single-PC installs need no lock.
    """
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:lock_key)"),
        {"lock_key": _CALIBRATION_ADVISORY_LOCK},
    ))

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


def calibration_is_excluded(record: PrintRecord, scope: str) -> bool:
    """Whether an operator excluded this card from one calibration scope.

    ``calibration_excluded`` is the legacy all-or-nothing switch. New records
    may use ``calibration_exclusions`` as either a list (``["scan", "time"]``)
    or a mapping (``{"scan": true}``). This matters for plates whose geometry
    is incomplete: they must not train scan/time models, while their printer
    log still contains perfectly valid recoat measurements.
    """
    meta = record.metadata_json or {}
    if meta.get("calibration_excluded"):
        return True
    exclusions = meta.get("calibration_exclusions") or []
    if isinstance(exclusions, dict):
        return bool(exclusions.get(scope) or exclusions.get("all"))
    if isinstance(exclusions, (list, tuple, set)):
        return scope in exclusions or "all" in exclusions
    return False


# Machine-time actuals are only trusted when the time_log covers (almost) the
# whole print: a partial log (multi-day rotation, truncated file) sums LESS
# machine time than the print really took and would drag the factor down.
_MACHINE_TIME_MIN_COVERAGE = 0.95
# Without a known expected layer count we cannot check coverage at all — a
# missing/legacy snapshot must not let a two-layer partial log pass as "the
# whole print". Require this many logged layers as a weak floor in that case;
# it does not replace the coverage check, only covers its absence.
_MACHINE_TIME_MIN_LAYERS_NO_EXPECTED = 100
_MACHINE_TIME_MAX_LAYER_RATIO = 1.05


def _has_full_layer_coverage(per_layer: dict[int, object], expected_layers: int | None) -> bool:
    """Whether timing rows plausibly cover the print from its first to last layer.

    Count alone is insufficient: a rotated log containing layers 6843..7016 can
    have enough rows for a short, wrongly-linked 174-layer prediction. We also
    require an anchored start and an end compatible with the expected count.
    """
    if not per_layer:
        return False
    layers = sorted(per_layer)
    if expected_layers:
        return (
            len(layers) >= _MACHINE_TIME_MIN_COVERAGE * expected_layers
            and layers[0] <= 2
            and layers[-1] >= _MACHINE_TIME_MIN_COVERAGE * expected_layers
            and layers[-1] <= _MACHINE_TIME_MAX_LAYER_RATIO * expected_layers
        )
    return len(layers) >= _MACHINE_TIME_MIN_LAYERS_NO_EXPECTED and layers[0] <= 2


def _machine_components_from_logs(
    session_id: str, expected_layers: int | None, db: Session,
) -> tuple[float, float] | None:
    """Return trusted ``(burn_hours, pour_hours)`` or None for partial logs."""
    from analytics.prediction.recoat_calibration import session_layer_seconds_by_layer

    per_layer = session_layer_seconds_by_layer(session_id, db)
    if not per_layer or not _has_full_layer_coverage(per_layer, expected_layers):
        return None
    burn = sum(values[0] for values in per_layer.values()) / 3600.0
    pour = sum(values[1] for values in per_layer.values()) / 3600.0
    return burn, pour


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
    components = _machine_components_from_logs(session_id, expected_layers, db)
    return sum(components) if components is not None else None


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
        select(PrintRecord)
        .where(PrintRecord.session_id.is_not(None))
        .order_by(PrintRecord.created_at, PrintRecord.record_id)
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


def _positive_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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

    Every linked pair remains visible for diagnosis, including a wall-clock
    fallback when machine timings are unavailable. Only complete machine logs,
    physics predictions with a scan/recoat breakdown, unique links and an
    explicit material+thickness mode can train a correction factor.
    """
    rows: list[dict] = []
    # (sort key, ratio) so the calibration window can be taken by recency.
    usable_by_mode: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    observed_modes: dict[str, tuple[str, float]] = {}
    observed_materials: set[str] = set()
    all_usable: list[tuple[datetime, float]] = []
    excluded: list[dict] = []

    linked = list(iter_linked_prints(db))
    link_counts = Counter(record.session_id for record, _ in linked)

    for record, session in linked:
        snapshot = (record.metadata_json or {}).get("prediction")
        if not snapshot:
            continue
        components = _machine_components_from_logs(
            record.session_id, snapshot.get("layer_count"), db,
        )
        if components is not None:
            actual_scan, actual_recoat = components
            actual = actual_scan + actual_recoat
            actual_source = "machine_log"
        else:
            actual_scan = actual_recoat = None
            actual = _actual_hours(session)
            actual_source = "wall_span" if actual is not None else None

        raw_total = _raw_predicted(snapshot)
        if actual is None or raw_total is None:
            continue

        material = (snapshot.get("material") or record.material or "—")
        observed_materials.add(material)
        thickness = _positive_number(snapshot.get("layer_thickness_mm"))
        mode = scan_model_key(material, thickness) if thickness is not None else None
        if mode is not None:
            observed_modes[mode] = (material, thickness)

        factor = float(snapshot.get("correction_factor") or 1.0) or 1.0
        # Preserve exactly what was quoted historically. For old snapshots
        # without print_hours, reproduce their old blanket-factor behaviour.
        shown = _positive_number(snapshot.get("print_hours")) or raw_total * factor
        raw_scan = _positive_number(snapshot.get("raw_scan_hours"))
        raw_recoat = _positive_number(snapshot.get("raw_recoat_hours"))
        shown_scan = _positive_number(snapshot.get("scan_hours"))
        shown_recoat = _positive_number(snapshot.get("recoat_hours"))
        ratio = actual_scan / raw_scan if actual_scan is not None and raw_scan else None
        skip_reason = _usable_for_calibration(session, actual)

        if skip_reason is None and calibration_is_excluded(record, "time"):
            skip_reason = "manually_excluded"
        if skip_reason is None and link_counts[record.session_id] > 1:
            skip_reason = "duplicate_session_link"
        if skip_reason is None and actual_source != "machine_log":
            skip_reason = "machine_time_unavailable"
        if skip_reason is None and snapshot.get("scan_source", "physics") != "physics":
            skip_reason = "already_fitted"
        if skip_reason is None and (raw_scan is None or raw_recoat is None):
            skip_reason = "missing_scan_breakdown"
        if skip_reason is None and mode is None:
            skip_reason = "missing_print_mode"
        if skip_reason is None and ratio is None:
            skip_reason = "missing_scan_actual"

        # Order pairs by when the print happened, so "most recent N" is real.
        when = printed_at(record, session)
        if skip_reason is None:
            usable_by_mode[mode].append((when, ratio))
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
            "mode": mode,
            "actual_hours": round(actual, 2),
            "actual_scan_hours": round(actual_scan, 3) if actual_scan is not None else None,
            "actual_recoat_hours": round(actual_recoat, 3) if actual_recoat is not None else None,
            # The corrected figure the operator saw — this is what "error" must
            # be measured against. The raw geometric hours are kept alongside it
            # because that is what the calibration ratio is computed from.
            "predicted_hours": round(shown, 2),
            "predicted_scan_hours": round(shown_scan, 3) if shown_scan is not None else None,
            "predicted_recoat_hours": round(shown_recoat, 3) if shown_recoat is not None else None,
            "raw_predicted_hours": round(raw_total, 2),
            "raw_scan_hours": round(raw_scan, 3) if raw_scan is not None else None,
            "raw_recoat_hours": round(raw_recoat, 3) if raw_recoat is not None else None,
            "correction_factor": round(factor, 3),
            "error_pct": round((shown - actual) / actual * 100, 1),
            "raw_error_pct": round((raw_total - actual) / actual * 100, 1),
            "scan_ratio": round(ratio, 3) if ratio is not None else None,
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

    by_mode = {}
    for mode, (material, thickness) in observed_modes.items():
        pairs = usable_by_mode.get(mode, [])
        by_mode[mode] = {
            "material": material,
            "layer_thickness_mm": thickness,
            "n_pairs": len(pairs),
            "suggested_factor": _median(pairs),
            "ratio_interval": _ratio_interval(pairs),
        }

    # Material rows are display summaries only. Never pool ratios from distinct
    # thicknesses into an applicable factor: when more than one mode is present
    # the factor and interval intentionally remain None.
    by_material = {}
    for material in sorted(observed_materials):
        modes = [key for key, info in by_mode.items() if info["material"] == material]
        mode_pairs = [pair for key in modes for pair in usable_by_mode.get(key, [])]
        single_mode = len(modes) == 1
        by_material[material] = {
            "n_pairs": len(mode_pairs),
            "modes": modes,
            "suggested_factor": _median(mode_pairs) if single_mode else None,
            "ratio_interval": _ratio_interval(mode_pairs) if single_mode else None,
        }

    return {
        "pairs": rows,
        "n_pairs": len(rows),
        "n_usable_pairs": len(all_usable),
        "excluded": excluded,
        "by_mode": by_mode,
        "by_material": by_material,
        # Overall median across materials — for the headline display only.
        "suggested_correction_factor": _median(all_usable),
        "min_pairs_for_calibration": MIN_PAIRS_FOR_CALIBRATION,
        "calibration_window": CALIBRATION_WINDOW,
    }


def calibration_interval_hours(
    db: Session, material: str, layer_thickness_mm: float,
    raw_scan_hours: float, raw_recoat_hours: float,
) -> tuple[float, float] | None:
    """Print-time interval for one mode, scaling scan while keeping recoat fixed.

    Scales the material's actual/raw ratio interval (``_ratio_interval`` inside
    ``prediction_accuracy``) by ``raw_hours`` — the same raw geometric estimate
    the ratio was computed against, so this must be called with the *raw*
    (uncorrected) hours, not the already-corrected quote.

    Returns ``None`` — never a fabricated interval — when the material has
    fewer than ``MIN_PAIRS_FOR_CALIBRATION`` usable pairs, exactly like the
    point correction factor.
    """
    report = prediction_accuracy(db)
    info = report["by_mode"].get(scan_model_key(material, layer_thickness_mm))
    if not info or info["ratio_interval"] is None:
        return None
    low, high = info["ratio_interval"]
    return (
        round(low * raw_scan_hours + raw_recoat_hours, 3),
        round(high * raw_scan_hours + raw_recoat_hours, 3),
    )


def recalibrate_and_apply(db: Session) -> dict:
    """Recompute per-material factors from history and persist the in-range ones.

    No-op when the operator has pinned factors (``correction_locked``). Returns a
    summary {applied: {...}, skipped: [...], locked: bool}. Caller's unit of work
    commits — this only mutates the row.
    """
    report = prediction_accuracy(db)
    by_mode = report["by_mode"]

    row = db.get(MachineParams, 1)
    if row is None:
        return {"applied": {}, "skipped": [], "locked": False, "reason": "no machine params"}
    if row.correction_locked:
        return {"applied": {}, "skipped": [], "locked": True}

    current = dict(row.time_correction_by_mat or {})
    applied: dict[str, float] = {}
    skipped: list[dict] = []
    for mode, info in by_mode.items():
        factor = info["suggested_factor"]
        if factor is None:
            continue  # not enough pairs yet
        if not (CORRECTION_MIN <= factor <= CORRECTION_MAX):
            skipped.append({"mode": mode, "factor": factor, "reason": "out_of_range"})
            logger.warning(
                "calibration: %s factor %.3f out of [%.1f, %.1f] — not applied "
                "(check machine params / orientation)",
                mode, factor, CORRECTION_MIN, CORRECTION_MAX,
            )
            continue
        if current.get(mode) != factor:
            logger.info("calibration: %s ×%s → ×%.3f (%d pairs)",
                        mode, current.get(mode), factor, info["n_pairs"])
            applied[mode] = factor

    # Unlocked factors are fully managed. Remove obsolete mode entries and the
    # old pooled-per-material entries so they cannot silently leak across modes.
    active_modes = {
        mode for mode, info in by_mode.items()
        if info["suggested_factor"] is not None
        and CORRECTION_MIN <= info["suggested_factor"] <= CORRECTION_MAX
    }
    observed_materials = set(report["by_material"])
    removed = sorted(
        key for key in current
        if ("@" in key and key not in active_modes) or key in observed_materials
    )
    for key in removed:
        current.pop(key, None)
    current.update(applied)
    if applied or removed:
        row.time_correction_by_mat = current
        row.updated_at = datetime.now(timezone.utc)

    return {"applied": applied, "removed": removed, "skipped": skipped, "locked": False}


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
