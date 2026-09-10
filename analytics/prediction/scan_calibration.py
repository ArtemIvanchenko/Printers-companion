"""Fit scan-time models from real per-layer burn_ms in printer logs (Level B).

Ground truth: ``*_time.log`` records, per physical layer, the machine's own
measured ``burn_ms`` (scan duration). Predictor: the co-hatched per-layer
geometry series the estimate stored in the prediction snapshot
(``metadata_json["prediction"]["scan_geometry"]``) at estimate time — pairing
them needs no re-slicing, just interpolation of the stored series at each
layer's height.

Model: per-layer ``burn_seconds ≈ Σ beta_k · g_k / laser_count + intercept``
over ``layer_engine.GEOMETRY_FEATURES``, fitted with non-negative least squares
(physics forbids negative time per unit length). Quality is reported from the
current database rather than frozen example numbers in source code.

Two hard-won honesty rules, both from real-data validation:

* The betas are NOT physical speeds. Geometry components are strongly
  collinear (they all grow with cross-section size), so NNLS concentrates
  weight arbitrarily among them. Only the fitted linear MAP is identifiable —
  never report ``1/beta`` as a speed.
* Models do NOT transfer across machines or modes. They are keyed by physical
  printer, material, layer thickness and laser count; legacy single-machine
  material/thickness keys remain readable.

Robustness to the known multi-day log-splitting bug: a session holding only
part of a print's layers still yields valid (geometry, burn) pairs for the
layers it has. Acceptance requires at least two independent geometry
fingerprints and leave-one-geometry-out validation, so an exact reprint cannot
appear in both train and validation. Normal controller cycle calibration is a
separate robust model over burn+pour→make and does not depend on accepted STL
scan geometry.
"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from analytics.prediction.accuracy import (
    PRINT_CLASSIFICATIONS,
    calibration_is_excluded,
    iter_linked_prints,
    session_classification,
)
from analytics.prediction.layer_engine import (
    GEOMETRY_FEATURES,
    LayerGeometrySeries,
    machine_mode_key,
)
from domain.enums.common import SourceFileFamily
from domain.models.prints import MachineParams

logger = logging.getLogger(__name__)

# Gates a fitted model must clear before it is ever used for predictions.
MIN_LAYERS_FOR_FIT = 150          # enough layers to constrain the fit
MIN_PRINTS_FOR_FIT = 2            # one print can only prove memorisation
MIN_FIT_R2 = 0.6                  # strong per-layer explanatory power
MAX_TOTAL_ERR_PCT = 15.0          # in-sample total must reconstruct within this
# Secondary acceptance channel, from real-data validation: a build whose
# geometry barely varies with height has almost no variance for R² to explain
# (a real 949-layer build fitted R²=0.26 while reconstructing its total to
# +0.6%) — rejecting it would discard a working model. Low-R² fits are accepted
# only when some geometric signal exists AND the total is tight. Pure noise
# fits R²≈0 and still fails.
MIN_FIT_R2_FLOOR = 0.2
TIGHT_TOTAL_ERR_PCT = 5.0
MAX_CV_MEDIAN_TOTAL_ERR_PCT = 25.0
MAX_CV_WORST_TOTAL_ERR_PCT = 35.0
# Plausibility bounds on a single layer's burn reading (ms) — mirrors the
# pour_ms guards in recoat_calibration.
_MIN_BURN_MS, _MAX_BURN_MS = 100.0, 3_600_000.0

# ``make_layer_ms`` is not simply burn+pour+one constant.  On real M-350
# builds the controller holds short layers near a minimum cycle duration.  The
# robust model below is therefore:
#
#   make = max(burn + pour + base_overhead, minimum_cycle)
#
# Extreme residuals are stops/restarts, not repeatable controller behaviour,
# and stay available in the raw stored row for diagnostics while being
# excluded from this fit. Moderate outliers are handled by the robust loss;
# the wider training guard must not delete legitimate floor-bound short layers.
_MAX_BASE_OVERHEAD_MS = 10_000.0
_MAX_MINIMUM_CYCLE_MS = 120_000.0
_MAX_CYCLE_TRAINING_RESIDUAL_MS = 60_000.0
_CYCLE_ROBUST_F_SCALE_MS = 500.0
_MIN_FLOOR_ROBUST_LOSS_IMPROVEMENT_PCT = 5.0
MIN_CYCLE_BRANCH_LAYERS = 30
MIN_CYCLE_BRANCH_PRINTS = 2


def _burn_seconds_by_layer(events: list[Any]) -> dict[int, float]:
    """{layer: burn_seconds}; conflicting repeated attempts are excluded."""
    from analytics.prediction.timing_validation import calibration_timing_payloads

    out: dict[int, float] = {}
    for payload in calibration_timing_payloads(events).values():
        layer, burn_ms = payload.get("layer"), payload.get("burn_ms")
        if not isinstance(layer, int) or not isinstance(burn_ms, (int, float)):
            continue
        if not (_MIN_BURN_MS <= burn_ms <= _MAX_BURN_MS):
            continue
        out[layer] = burn_ms / 1000.0
    return out


def _layer_overhead_ms_by_layer(events: list[Any]) -> dict[int, float]:
    """Validated ``make_layer - burn - pour`` residual per physical layer."""
    from analytics.prediction.layer_timings import MAX_LAYER_OVERHEAD_MS

    out: dict[int, float] = {}
    for layer, (burn_ms, pour_ms, make_layer_ms) in _layer_cycles_ms_by_layer(events).items():
        overhead_ms = make_layer_ms - burn_ms - pour_ms
        if 0.0 <= overhead_ms <= MAX_LAYER_OVERHEAD_MS:
            out[layer] = overhead_ms
    return out


def _layer_cycles_ms_by_layer(
    events: list[Any],
) -> dict[int, tuple[float, float, float]]:
    """Raw valid cycles; conflicting repeated attempts are excluded.

    Unlike ``_layer_overhead_ms_by_layer`` this deliberately retains a long
    pause-like ``make`` value.  It is evidence and may be useful to explain the
    finished build; only the normal-cycle fitter filters it out.
    """
    from analytics.prediction.recoat_calibration import _MAX_POUR_MS, _MIN_POUR_MS
    from analytics.prediction.timing_validation import calibration_timing_payloads

    out: dict[int, tuple[float, float, float]] = {}
    for payload in calibration_timing_payloads(events).values():
        layer = payload.get("layer")
        values = [payload.get(name) for name in ("burn_ms", "pour_ms", "make_layer_ms")]
        if not isinstance(layer, int) or not all(
            isinstance(value, (int, float)) for value in values
        ):
            continue
        burn_ms, pour_ms, make_layer_ms = (float(value) for value in values)
        if not (_MIN_BURN_MS <= burn_ms <= _MAX_BURN_MS):
            continue
        if not (_MIN_POUR_MS <= pour_ms <= _MAX_POUR_MS):
            continue
        if make_layer_ms < burn_ms + pour_ms:
            continue
        out[layer] = (burn_ms, pour_ms, make_layer_ms)
    return out


def session_burn_by_layer(session_id: str, db: Session) -> dict[int, float] | None:
    """Real per-layer burn seconds for one session.

    Prefers the stored per-layer conclusions (see layer_timings — they survive
    the file not being on this machine, which is the shared-database case);
    falls back to re-parsing the log for sessions imported before storage.
    """
    from analytics.prediction.layer_timings import stored_timings

    stored = stored_timings(session_id, db)
    if stored:
        return {layer: burn / 1000.0 for layer, (burn, _) in stored.items()}

    from storage.repositories.runtime import RuntimeRepository
    from domain.services.compute_affinity import ComputeAffinityError

    try:
        files = RuntimeRepository(db).get_session_files(session_id, rehydrate=True)
    except ComputeAffinityError:
        # Global calibration may consume normalized rows from every PC, but it
        # must never fetch/reparse another workstation's raw logs.
        return None
    if not files:
        return None
    events: list[Any] = []
    for f in files:
        if f.classification.family != SourceFileFamily.time_log or not f.parse_result:
            continue
        events.extend(f.parse_result.events)
    out = _burn_seconds_by_layer(events)
    return out or None


def session_layer_overhead_ms_by_layer(session_id: str, db: Session) -> dict[int, float] | None:
    """Inter-phase machine overhead by layer, from DB first, local log second."""
    from analytics.prediction.layer_timings import stored_layer_overheads

    stored = stored_layer_overheads(session_id, db)
    if stored:
        return stored

    from storage.repositories.runtime import RuntimeRepository
    from domain.services.compute_affinity import ComputeAffinityError

    try:
        files = RuntimeRepository(db).get_session_files(session_id, rehydrate=True)
    except ComputeAffinityError:
        return None
    if not files:
        return None
    events: list[Any] = []
    for file in files:
        if file.classification.family != SourceFileFamily.time_log or not file.parse_result:
            continue
        events.extend(file.parse_result.events)
    out = _layer_overhead_ms_by_layer(events)
    return out or None


def session_layer_cycles_ms_by_layer(
    session_id: str, db: Session,
) -> dict[int, tuple[float, float, float]] | None:
    """Raw complete layer cycles, from shared DB first and local log second."""
    from analytics.prediction.layer_timings import stored_layer_cycles

    stored = stored_layer_cycles(session_id, db)
    if stored:
        return stored

    from domain.services.compute_affinity import ComputeAffinityError
    from storage.repositories.runtime import RuntimeRepository

    try:
        files = RuntimeRepository(db).get_session_files(session_id, rehydrate=True)
    except ComputeAffinityError:
        return None
    if not files:
        return None
    events: list[Any] = []
    for file in files:
        if file.classification.family != SourceFileFamily.time_log or not file.parse_result:
            continue
        events.extend(file.parse_result.events)
    out = _layer_cycles_ms_by_layer(events)
    return out or None


def _pairs_from_record(snapshot: dict, burn: dict[int, float]) -> tuple[list[list[float]], list[float]] | None:
    """(X rows, y) for one record: stored geometry interpolated at each layer."""
    geo = snapshot.get("scan_geometry")
    if not isinstance(geo, dict):
        return None
    thickness = geo.get("layer_thickness_mm")
    laser_count = int(geo.get("laser_count") or 1)
    if not thickness or thickness <= 0:
        return None
    try:
        series = LayerGeometrySeries.from_snapshot(geo)
    except (KeyError, TypeError, ValueError):
        return None

    X_rows, y = [], []
    for layer, burn_s in sorted(burn.items()):
        z = series.z_min + (layer - 0.5) * thickness
        if not (series.zs[0] <= z <= series.zs[-1]):
            continue
        g = series.at(z)
        X_rows.append([v / max(laser_count, 1) for v in g] + [1.0])
        y.append(burn_s)
    if not X_rows:
        return None
    return X_rows, y


def _geometry_fingerprint(snapshot: dict) -> str | None:
    """Stable geometry identity for leakage-safe validation.

    New snapshots carry the SHA built from sorted ``file_type + file SHA``.
    Old snapshots predate that field, so their canonical stored geometry is a
    conservative fallback: exact reprints remain in the same CV fold instead
    of leaking a twin into both train and validation.
    """
    value = snapshot.get("geometry_fingerprint")
    if isinstance(value, str) and value.strip():
        return value.strip()
    geometry = snapshot.get("scan_geometry")
    if not isinstance(geometry, dict):
        return None
    canonical = json.dumps(
        geometry, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    ).encode("utf-8")
    return "legacy-scan-geometry:" + hashlib.sha256(canonical).hexdigest()


def _group_weights(
    groups: list[tuple[list[Any], list[float]]],
    geometry_fingerprints: list[str],
) -> list[float]:
    """Per-group row multiplier: equal geometry, then equal print weight."""
    counts = Counter(geometry_fingerprints)
    total_rows = sum(len(y) for _, y in groups)
    n_geometries = max(len(counts), 1)
    weights: list[float] = []
    for (_, y), fingerprint in zip(groups, geometry_fingerprints):
        # Sum of squared weights is equal for each geometry cluster; within a
        # cluster it is equal for each physical print, regardless of layers.
        denominator = n_geometries * counts[fingerprint] * max(len(y), 1)
        weights.append((max(total_rows, 1) / denominator) ** 0.5)
    return weights


def _fit_beta(
    groups: list[tuple[list[list[float]], list[float]]],
    geometry_fingerprints: list[str] | None = None,
):
    """Fit NNLS with equal geometry-cluster, then equal-print weight."""
    import numpy as np
    from scipy.optimize import nnls

    fingerprints = geometry_fingerprints or [f"group:{i}" for i in range(len(groups))]
    weights = _group_weights(groups, fingerprints)
    X_weighted: list[list[float]] = []
    y_weighted: list[float] = []
    for (X_rows, y), row_weight in zip(groups, weights):
        if not y:
            continue
        for row, val in zip(X_rows, y):
            X_weighted.append([v * row_weight for v in row])
            y_weighted.append(val * row_weight)
    if not X_weighted:
        return None
    try:
        beta, _ = nnls(np.asarray(X_weighted, dtype=float), np.asarray(y_weighted, dtype=float))
    except Exception:
        logger.exception("scan calibration: NNLS failed")
        return None
    return beta


def _fit(
    groups: list[tuple[list[list[float]], list[float]]],
    geometry_fingerprints: list[str] | None = None,
) -> dict[str, Any] | None:
    """NNLS fit with leakage-safe leave-one-geometry-out validation.

    Geometry clusters receive equal fit weight, then physical prints within a
    cluster receive equal weight regardless of their layer count. R² and
    total error remain unweighted descriptions of the observed rows.
    """
    import numpy as np

    X_all: list[list[float]] = []
    y_all: list[float] = []
    for X_rows, y in groups:
        if not y:
            continue
        for row, val in zip(X_rows, y):
            X_all.append(row)
            y_all.append(val)
    if not X_all:
        return None

    X = np.asarray(X_all, dtype=float)
    yv = np.asarray(y_all, dtype=float)
    fingerprints = geometry_fingerprints or [f"group:{i}" for i in range(len(groups))]
    beta = _fit_beta(groups, fingerprints)
    if beta is None:
        return None
    pred = X @ beta
    ss_res = float(((yv - pred) ** 2).sum())
    ss_tot = float(((yv - yv.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    total_err_pct = (float(pred.sum()) - float(yv.sum())) / float(yv.sum()) * 100.0 if yv.sum() else 0.0
    model = {
        "beta": [float(b) for b in beta],
        "features": list(GEOMETRY_FEATURES),
        "r2": round(r2, 4),
        "total_err_pct": round(total_err_pct, 2),
        "n_layers": len(y_all),
        "n_prints": len(groups),
        "n_geometries": len(set(fingerprints)),
        "fitted_at": datetime.now(timezone.utc).isoformat(),
    }

    cv_errors: list[float] = []
    # Leave one unique geometry out, never one record.  Exact reprints are
    # useful repeated measurements in the final fit, but putting one twin in
    # train and one in validation would report memorisation as generalisation.
    if len(set(fingerprints)) >= 2:
        for holdout_fingerprint in sorted(set(fingerprints)):
            train_indices = [
                index for index, value in enumerate(fingerprints)
                if value != holdout_fingerprint
            ]
            holdout_indices = [
                index for index, value in enumerate(fingerprints)
                if value == holdout_fingerprint
            ]
            train = [groups[index] for index in train_indices]
            train_fingerprints = [fingerprints[index] for index in train_indices]
            fold_beta = _fit_beta(train, train_fingerprints)
            if fold_beta is None:
                continue
            X_holdout = [row for index in holdout_indices for row in groups[index][0]]
            y_holdout = [value for index in holdout_indices for value in groups[index][1]]
            if not y_holdout:
                continue
            fold_pred = np.asarray(X_holdout, dtype=float) @ fold_beta
            actual_total = float(np.asarray(y_holdout, dtype=float).sum())
            if actual_total > 0:
                cv_errors.append((float(fold_pred.sum()) - actual_total) / actual_total * 100.0)
    model["cv_total_errors_pct"] = [round(value, 2) for value in cv_errors]
    model["cv_median_abs_total_err_pct"] = (
        round(statistics.median(abs(value) for value in cv_errors), 2)
        if cv_errors else None
    )
    model["cv_worst_abs_total_err_pct"] = (
        round(max(abs(value) for value in cv_errors), 2) if cv_errors else None
    )
    return model


def _gate(model: dict[str, Any]) -> str | None:
    """Reason this fit must not be used, or None if it passes."""
    if model["n_prints"] < MIN_PRINTS_FOR_FIT:
        return f"too_few_prints ({model['n_prints']} < {MIN_PRINTS_FOR_FIT})"
    if model.get("n_geometries", 0) < 2:
        return f"too_few_unique_geometries ({model.get('n_geometries', 0)} < 2)"
    if model["n_layers"] < MIN_LAYERS_FOR_FIT:
        return f"too_few_layers ({model['n_layers']} < {MIN_LAYERS_FOR_FIT})"
    if abs(model["total_err_pct"]) > MAX_TOTAL_ERR_PCT:
        return f"total_err ({model['total_err_pct']}% > {MAX_TOTAL_ERR_PCT}%)"
    cv_median = model.get("cv_median_abs_total_err_pct")
    cv_worst = model.get("cv_worst_abs_total_err_pct")
    if cv_median is None or cv_median > MAX_CV_MEDIAN_TOTAL_ERR_PCT:
        return f"cv_median_total_err ({cv_median}% > {MAX_CV_MEDIAN_TOTAL_ERR_PCT}%)"
    if cv_worst is None or cv_worst > MAX_CV_WORST_TOTAL_ERR_PCT:
        return f"cv_worst_total_err ({cv_worst}% > {MAX_CV_WORST_TOTAL_ERR_PCT}%)"
    if model["r2"] >= MIN_FIT_R2:
        return None
    if model["r2"] >= MIN_FIT_R2_FLOOR and abs(model["total_err_pct"]) <= TIGHT_TOTAL_ERR_PCT:
        return None  # low-variance build: weak per-layer signal, tight total
    return f"low_r2 ({model['r2']} < {MIN_FIT_R2})"


def _fit_layer_cycle_model(
    groups: list[tuple[list[float], list[float]]],
    geometry_fingerprints: list[str],
) -> dict[str, Any] | None:
    """Fit ``make=max(burn+pour+base, floor)`` without pause-like rows.

    A floor is published only when both sides of the kink are independently
    observed.  Otherwise infinitely many floor values can have identical
    loss; in that case the honest fallback is the one-parameter additive base
    model and ``minimum_cycle_ms=None``.
    """
    import numpy as np
    from scipy.optimize import least_squares

    clean_groups: list[tuple[list[float], list[float]]] = []
    clean_fingerprints: list[str] = []
    pause_like_rows = 0
    for (components, makes), fingerprint in zip(groups, geometry_fingerprints):
        clean_components: list[float] = []
        clean_makes: list[float] = []
        for component_ms, make_ms in zip(components, makes):
            residual_ms = make_ms - component_ms
            if not (0.0 <= residual_ms <= _MAX_CYCLE_TRAINING_RESIDUAL_MS):
                pause_like_rows += 1
                continue
            clean_components.append(float(component_ms))
            clean_makes.append(float(make_ms))
        if clean_makes:
            clean_groups.append((clean_components, clean_makes))
            clean_fingerprints.append(fingerprint)
    if not clean_groups:
        return None

    row_weights = _group_weights(clean_groups, clean_fingerprints)
    x = np.asarray(
        [value for components, _ in clean_groups for value in components], dtype=float,
    )
    y = np.asarray([value for _, makes in clean_groups for value in makes], dtype=float)
    weights = np.asarray([
        weight
        for (_, makes), weight in zip(clean_groups, row_weights)
        for _ in makes
    ], dtype=float)
    residuals = y - x
    base_start = float(np.clip(np.median(residuals), 0.0, _MAX_BASE_OVERHEAD_MS))
    floor_start = float(np.clip(
        np.percentile(y, 20), 0.0, _MAX_MINIMUM_CYCLE_MS,
    ))

    def kink_residual(theta):
        base_ms, floor_ms = theta
        return (np.maximum(x + base_ms, floor_ms) - y) * weights

    try:
        fitted = least_squares(
            kink_residual,
            x0=np.asarray([base_start, floor_start]),
            bounds=(
                np.asarray([0.0, 0.0]),
                np.asarray([_MAX_BASE_OVERHEAD_MS, _MAX_MINIMUM_CYCLE_MS]),
            ),
            loss="soft_l1",
            f_scale=_CYCLE_ROBUST_F_SCALE_MS,
        )
    except Exception:
        logger.exception("layer-cycle calibration: robust fit failed")
        return None

    base_ms, candidate_floor_ms = (float(value) for value in fitted.x)

    def base_residual(theta):
        return (x + theta[0] - y) * weights

    base_only = least_squares(
        base_residual,
        x0=np.asarray([base_start]),
        bounds=(np.asarray([0.0]), np.asarray([_MAX_BASE_OVERHEAD_MS])),
        loss="soft_l1",
        f_scale=_CYCLE_ROBUST_F_SCALE_MS,
    )
    base_only_ms = float(base_only.x[0])
    floor_loss_improvement_pct = (
        max(0.0, (float(base_only.cost) - float(fitted.cost)) / float(base_only.cost) * 100.0)
        if base_only.cost > 0.0 else 0.0
    )

    def branch_counts(floor_ms: float) -> tuple[int, int, int, int, int, int]:
        floor_layers = floor_prints = free_layers = free_prints = 0
        floor_geometries: set[str] = set()
        free_geometries: set[str] = set()
        for (components, _), fingerprint in zip(clean_groups, clean_fingerprints):
            group_floor = sum(
                floor_ms > component_ms + base_ms + 1.0 for component_ms in components
            )
            group_free = len(components) - group_floor
            floor_layers += group_floor
            free_layers += group_free
            if group_floor:
                floor_prints += 1
                floor_geometries.add(fingerprint)
            if group_free:
                free_prints += 1
                free_geometries.add(fingerprint)
        return (
            floor_layers, floor_prints, len(floor_geometries),
            free_layers, free_prints, len(free_geometries),
        )

    (
        floor_layers, floor_prints, floor_geometries,
        free_layers, free_prints, free_geometries,
    ) = branch_counts(candidate_floor_ms)
    floor_identified = (
        floor_layers >= MIN_CYCLE_BRANCH_LAYERS
        and floor_prints >= MIN_CYCLE_BRANCH_PRINTS
        and floor_geometries >= 2
        and free_layers >= MIN_CYCLE_BRANCH_LAYERS
        and free_prints >= MIN_CYCLE_BRANCH_PRINTS
        and free_geometries >= 2
        and floor_loss_improvement_pct >= _MIN_FLOOR_ROBUST_LOSS_IMPROVEMENT_PCT
    )

    if floor_identified:
        minimum_cycle_ms: float | None = candidate_floor_ms
        predicted = np.maximum(x + base_ms, minimum_cycle_ms)
        floor_status = "identified"
    else:
        # When the kink is unsupported, refit the sole identifiable parameter
        # instead of letting an arbitrary optimiser start value leak into API.
        base_ms = base_only_ms
        minimum_cycle_ms = None
        predicted = x + base_ms
        floor_status = "unidentified"

    absolute_error = np.abs(predicted - y)
    total_error_pct = (
        (float(predicted.sum()) - float(y.sum())) / float(y.sum()) * 100.0
        if y.sum() else 0.0
    )
    return {
        "version": "max_base_floor_v1",
        "base_overhead_ms": round(base_ms, 3),
        "minimum_cycle_ms": (
            round(minimum_cycle_ms, 3) if minimum_cycle_ms is not None else None
        ),
        "minimum_cycle_status": floor_status,
        "floor_robust_loss_improvement_pct": round(floor_loss_improvement_pct, 3),
        "n_prints": len(clean_groups),
        "n_layers": int(len(y)),
        "n_geometries": len(set(clean_fingerprints)),
        "floor_n_prints": floor_prints,
        "floor_n_layers": floor_layers,
        "floor_n_geometries": floor_geometries,
        "free_n_prints": free_prints,
        "free_n_layers": free_layers,
        "free_n_geometries": free_geometries,
        "pause_like_rows_excluded": pause_like_rows,
        "median_absolute_error_ms": round(float(np.median(absolute_error)), 3),
        "total_error_pct": round(total_error_pct, 3),
        "loss": "scipy_least_squares_soft_l1_equal_geometry_print_weight",
        "source": "make_layer_ms_vs_burn_plus_pour",
        "fitted_at": datetime.now(timezone.utc).isoformat(),
    }


def scan_calibration_report(db: Session) -> dict:
    """Collect (geometry, burn) pairs per mode and fit candidate models."""
    rows: list[dict] = []
    # One (X_rows, y) group per contributing print, not a flat pool — _fit
    # weights each print's group equally regardless of its own layer count.
    by_key: dict[
        str, list[tuple[list[list[float]], list[float], str, str]]
    ] = defaultdict(list)
    cycle_by_key: dict[
        str, list[tuple[list[float], list[float], str, str]]
    ] = defaultdict(list)

    linked = list(iter_linked_prints(db))
    link_counts = Counter(record.session_id for record, _ in linked)
    for record, session in linked:
        snapshot = (record.metadata_json or {}).get("prediction") or {}
        if session_classification(session) not in PRINT_CLASSIFICATIONS:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "not_a_print"})
            continue
        if link_counts[record.session_id] > 1:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "duplicate_session_link"})
            continue

        geo = snapshot.get("scan_geometry")
        material = (snapshot.get("material") or record.material or "—")
        thickness_value = snapshot.get("layer_thickness_mm")
        if thickness_value is None and isinstance(geo, dict):
            thickness_value = geo.get("layer_thickness_mm")
        if thickness_value is None:
            thickness_value = record.layer_thickness_mm
        try:
            thickness = float(thickness_value)
        except (TypeError, ValueError):
            thickness = 0.0
        laser_value = snapshot.get("laser_count")
        if laser_value is None and isinstance(geo, dict):
            laser_value = geo.get("laser_count")
        try:
            laser_count = max(int(laser_value or 1), 1)
        except (TypeError, ValueError):
            laser_count = 1
        printer_id = snapshot.get("printer_id") or session.printer_id
        key = (
            machine_mode_key(
                str(printer_id) if printer_id else None,
                material,
                thickness,
                laser_count,
            )
            if thickness > 0 else None
        )

        # Full-cycle calibration needs only machine timings and a mode. It is
        # intentionally independent of STL completeness and NNLS scan gates.
        cycles = (
            None if calibration_is_excluded(record, "cycle")
            else session_layer_cycles_ms_by_layer(record.session_id, db)
        )
        cycle_used = False
        if cycles and key is not None:
            cycle_fingerprint = _geometry_fingerprint(snapshot) or "unknown-geometry"
            cycle_by_key[key].append((
                [burn_ms + pour_ms for burn_ms, pour_ms, _ in cycles.values()],
                [make_ms for _, _, make_ms in cycles.values()],
                record.record_id,
                cycle_fingerprint,
            ))
            cycle_used = True

        if calibration_is_excluded(record, "scan"):
            rows.append({
                "record_id": record.record_id,
                "session_id": record.session_id,
                "used": False,
                "cycle_used": cycle_used,
                "reason": "manually_excluded",
            })
            continue

        if not isinstance(geo, dict):
            rows.append({
                "record_id": record.record_id,
                "session_id": record.session_id,
                "used": False,
                "cycle_used": cycle_used,
                "reason": "no_scan_geometry",
            })
            continue
        burn = session_burn_by_layer(record.session_id, db)
        if not burn:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "no_time_log"})
            continue
        pairs = _pairs_from_record(snapshot, burn)
        if pairs is None:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "geometry_snapshot_unusable"})
            continue

        if key is None:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "cycle_used": cycle_used,
                         "reason": "mode_unavailable"})
            continue
        fingerprint = _geometry_fingerprint(snapshot)
        if fingerprint is None:
            rows.append({"record_id": record.record_id, "session_id": record.session_id,
                         "used": False, "reason": "geometry_fingerprint_unavailable"})
            continue
        by_key[key].append((pairs[0], pairs[1], record.record_id, fingerprint))
        rows.append({"record_id": record.record_id, "session_id": record.session_id,
                     "used": True, "mode": key, "n_layers": len(pairs[1]),
                     "cycle_used": cycle_used, "geometry_fingerprint": fingerprint})

    candidates: dict[str, dict] = {}
    for key, groups in by_key.items():
        model = _fit(
            [(X_rows, y) for X_rows, y, _, _ in groups],
            [fingerprint for _, _, _, fingerprint in groups],
        )
        if model is None:
            candidates[key] = {"status": "fit_failed"}
            continue
        model["source_records"] = [rid for _, _, rid, _ in groups]
        model["source_geometry_fingerprints"] = sorted({
            fingerprint for _, _, _, fingerprint in groups
        })
        reason = _gate(model)
        model["status"] = "ok" if reason is None else f"rejected: {reason}"
        candidates[key] = model

    cycle_candidates: dict[str, dict] = {}
    for key, groups in cycle_by_key.items():
        cycle_model = _fit_layer_cycle_model(
            [(components, makes) for components, makes, _, _ in groups],
            [fingerprint for _, _, _, fingerprint in groups],
        )
        if cycle_model is None:
            cycle_candidates[key] = {"status": "fit_failed"}
            continue
        cycle_model["source_records"] = [record_id for _, _, record_id, _ in groups]
        if cycle_model["n_prints"] < MIN_PRINTS_FOR_FIT:
            status = (
                f"rejected: too_few_prints ({cycle_model['n_prints']} < "
                f"{MIN_PRINTS_FOR_FIT})"
            )
        elif cycle_model["n_geometries"] < 2:
            status = (
                "rejected: too_few_unique_geometries "
                f"({cycle_model['n_geometries']} < 2)"
            )
        elif cycle_model["n_layers"] < MIN_LAYERS_FOR_FIT:
            status = (
                f"rejected: too_few_layers ({cycle_model['n_layers']} < "
                f"{MIN_LAYERS_FOR_FIT})"
            )
        else:
            status = "ok"
        cycle_model["status"] = status
        cycle_candidates[key] = cycle_model

    return {
        "records": rows,
        "candidates": candidates,
        "cycle_candidates": cycle_candidates,
        "min_layers_for_fit": MIN_LAYERS_FOR_FIT,
        "min_prints_for_fit": MIN_PRINTS_FOR_FIT,
        "min_r2": MIN_FIT_R2,
        "max_total_err_pct": MAX_TOTAL_ERR_PCT,
    }


def recalibrate_scan_and_apply(db: Session) -> dict:
    """Fit per-mode scan models from history and persist the ones passing gates.

    No-op when ``correction_locked`` (one operator lock for all auto-calibration).
    Caller commits.
    """
    report = scan_calibration_report(db)

    row = db.get(MachineParams, 1)
    if row is None:
        return {"applied": {}, "skipped": [], "locked": False, "reason": "no machine params"}
    if row.correction_locked:
        return {"applied": {}, "skipped": [], "locked": True}

    current = dict(row.scan_model_by_mat or {})
    current_cycles = dict(row.layer_cycle_model_by_mode or {})
    applied: dict[str, dict] = {}
    skipped: list[dict] = []
    removed: list[str] = []
    for key, model in report["candidates"].items():
        if model.get("status") != "ok":
            skipped.append({"mode": key, "reason": model.get("status", "unknown")})
            # A previously-fitted model for this mode is now unsupported by the
            # current data (e.g. a record's material/thickness was corrected,
            # or the pool grew and no longer clears the gate) — a stale model
            # would otherwise sit in scan_model_by_mat forever and keep being
            # applied to new estimates as if still valid.
            if key in current:
                del current[key]
                removed.append(key)
            continue
        stored = {k: v for k, v in model.items() if k != "status"}
        if current.get(key) != stored:
            logger.info("scan calibration: %s fitted (r2=%.3f, n=%d, total_err=%+.1f%%)",
                        key, model["r2"], model["n_layers"], model["total_err_pct"])
            applied[key] = stored

    # Models are auto-managed when unlocked. A key that is no longer backed by
    # any candidate (records deleted/reclassified/excluded) is stale too.
    for key in list(current):
        if key not in report["candidates"] and key not in removed:
            del current[key]
            removed.append(key)

    if removed:
        logger.info("scan calibration: removed stale model(s) no longer supported: %s", removed)
    if applied or removed:
        current.update(applied)
        row.scan_model_by_mat = current
        row.updated_at = datetime.now(timezone.utc)

    cycle_applied: dict[str, dict] = {}
    cycle_skipped: list[dict] = []
    cycle_removed: list[str] = []
    for key, model in report.get("cycle_candidates", {}).items():
        if model.get("status") != "ok":
            cycle_skipped.append({"mode": key, "reason": model.get("status", "unknown")})
            if key in current_cycles:
                del current_cycles[key]
                cycle_removed.append(key)
            continue
        stored = {field: value for field, value in model.items() if field != "status"}
        if current_cycles.get(key) != stored:
            cycle_applied[key] = stored
    for key in list(current_cycles):
        if key not in report.get("cycle_candidates", {}) and key not in cycle_removed:
            del current_cycles[key]
            cycle_removed.append(key)
    if cycle_applied or cycle_removed:
        current_cycles.update(cycle_applied)
        row.layer_cycle_model_by_mode = current_cycles
        row.updated_at = datetime.now(timezone.utc)

    return {
        "applied": applied,
        "skipped": skipped,
        "removed": removed,
        "cycle_applied": cycle_applied,
        "cycle_skipped": cycle_skipped,
        "cycle_removed": cycle_removed,
        "locked": False,
    }


__all__ = [
    "scan_calibration_report",
    "recalibrate_scan_and_apply",
    "session_burn_by_layer",
    "session_layer_overhead_ms_by_layer",
    "session_layer_cycles_ms_by_layer",
    "_layer_overhead_ms_by_layer",
    "_layer_cycles_ms_by_layer",
    "_fit_layer_cycle_model",
    "MIN_LAYERS_FOR_FIT",
    "MIN_PRINTS_FOR_FIT",
    "MIN_FIT_R2",
    "MIN_FIT_R2_FLOOR",
    "MAX_TOTAL_ERR_PCT",
    "TIGHT_TOTAL_ERR_PCT",
    "MAX_CV_MEDIAN_TOTAL_ERR_PCT",
    "MAX_CV_WORST_TOTAL_ERR_PCT",
    "MIN_CYCLE_BRANCH_LAYERS",
    "MIN_CYCLE_BRANCH_PRINTS",
]
