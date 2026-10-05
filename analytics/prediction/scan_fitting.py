"""Local numerical fits and acceptance gates for scan/cycle calibration.

This module consumes copied groups of measurements, never a database session,
files or the NAS. Equations, thresholds and persisted model fields are the same
as the original scan_calibration implementation. Scan uses geometry-held-out
validation; the cycle fitter remains an in-sample fit, not a held-out proof.
"""
from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from analytics.prediction.layer_engine import GEOMETRY_FEATURES

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
# ``make_layer_ms`` is not simply burn+pour+one constant.  On real M-350
# builds the controller holds short layers near a minimum cycle duration.  The
# robust model below is therefore:
#
#   make = max(burn + pour + base_overhead, minimum_cycle)
#
# Large residuals remain diagnostic and are excluded by a provisional guard,
# not established as stops. A machine with a genuine large cycle floor needs
# a separately confirmed admission profile, not a guessed replacement label.
_MAX_BASE_OVERHEAD_MS = 10_000.0
_MAX_MINIMUM_CYCLE_MS = 120_000.0
_MAX_CYCLE_TRAINING_RESIDUAL_MS = 60_000.0
_CYCLE_ROBUST_F_SCALE_MS = 500.0
_MIN_FLOOR_ROBUST_LOSS_IMPROVEMENT_PCT = 5.0
MIN_CYCLE_BRANCH_LAYERS = 30
MIN_CYCLE_BRANCH_PRINTS = 2


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
            # One error per physical print: opposite errors of exact reprints
            # must not cancel before the absolute-error acceptance gate.
            for index in holdout_indices:
                X_holdout, y_holdout = groups[index]
                if not y_holdout:
                    continue
                fold_pred = np.asarray(X_holdout, dtype=float) @ fold_beta
                actual_total = float(np.asarray(y_holdout, dtype=float).sum())
                if actual_total > 0:
                    cv_errors.append((float(fold_pred.sum()) - actual_total) / actual_total * 100.0)
    model["cv_error_unit"] = "print_with_geometry_held_out"
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

    Neither a flat floor-only sample nor an unsupported kink identifies a
    universally additive base. Unidentified parameters remain null and must
    not be published. A free-only fit has a lower applicability boundary.
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
            if (isinstance(component_ms, bool) or isinstance(make_ms, bool)
                    or not isinstance(component_ms, (int, float))
                    or not isinstance(make_ms, (int, float))
                    or not np.isfinite(component_ms) or not np.isfinite(make_ms)
                    or component_ms < 0 or make_ms <= 0):
                pause_like_rows += 1
                continue
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
        base_status = "identified"
        minimum_component_ms = None
    else:
        # An additive model is supported only by varying free-branch data with
        # no material evidence favouring a kink. Flat floor-only observations
        # cannot identify a base even when their additive approximation fits.
        free_identified = (
            free_layers >= MIN_CYCLE_BRANCH_LAYERS
            and free_prints >= MIN_CYCLE_BRANCH_PRINTS and free_geometries >= 2
            and float(np.percentile(x, 90) - np.percentile(x, 10)) >= 2 * _CYCLE_ROBUST_F_SCALE_MS
            and floor_loss_improvement_pct < _MIN_FLOOR_ROBUST_LOSS_IMPROVEMENT_PCT
            and float(np.median(np.abs(x + base_only_ms - y))) <= 3 * _CYCLE_ROBUST_F_SCALE_MS
        )
        base_ms = base_only_ms if free_identified else None
        base_status = "identified_free_branch" if free_identified else "unidentified"
        minimum_cycle_ms = None
        minimum_component_ms = float(x.min()) if free_identified else None
        # Fit diagnostics are not usable parameters and do not authorise reuse.
        predicted = x + base_only_ms if free_identified else np.maximum(x + fitted.x[0], candidate_floor_ms)
        floor_status = "unidentified"

    absolute_error = np.abs(predicted - y)
    total_error_pct = (
        (float(predicted.sum()) - float(y.sum())) / float(y.sum()) * 100.0
        if y.sum() else 0.0
    )
    return {
        "version": "max_base_floor_v2",
        "base_overhead_ms": round(base_ms, 3) if base_ms is not None else None,
        "base_overhead_status": base_status,
        "minimum_applicable_component_ms": minimum_component_ms,
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
