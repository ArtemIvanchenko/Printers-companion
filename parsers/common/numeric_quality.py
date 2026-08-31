from __future__ import annotations

import math
import warnings
from collections import Counter
from statistics import median
from typing import Any

def _longest_constant_run(values: list[float | None]) -> int:
    longest = current = 0
    previous: float | None = None
    for value in values:
        if value is not None and previous is not None and value == previous:
            current += 1
        elif value is not None:
            current = 1
        else:
            current = 0
        longest = max(longest, current)
        previous = value
    return longest


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def robust_numeric_profile(
    rows: list[dict[str, Any]],
    *,
    ignored_columns: set[str] | None = None,
    z_threshold: float = 6.0,
) -> dict[str, Any]:
    """Describe completeness, spikes and frozen channels without distribution assumptions."""
    ignored = ignored_columns or set()
    columns = sorted({key for row in rows for key in row} - ignored)
    summaries: dict[str, dict[str, Any]] = {}
    numeric_cells = expected_cells = spike_cells = 0
    for column in columns:
        series = [
            float(value) if isinstance(value, int | float) and math.isfinite(float(value)) else None
            for value in (row.get(column) for row in rows)
        ]
        numeric = [value for value in series if value is not None]
        if not numeric:
            continue
        expected_cells += len(series)
        numeric_cells += len(numeric)
        center = float(median(numeric))
        mad = float(median(abs(value - center) for value in numeric))
        scale = 1.4826 * mad
        if scale > 1e-12:
            spikes = sum(abs(value - center) / scale > z_threshold for value in numeric)
        else:
            # A zero MAD is common for discrete/idle channels.  Treating every
            # legitimate state change as a spike would badly inflate the score;
            # such channels are described by their frozen run instead.
            spikes = 0
        spike_cells += spikes
        summaries[column] = {
            "count": int(len(numeric)),
            "missing_fraction": round(1.0 - len(numeric) / max(len(series), 1), 6),
            "median": center,
            "mad": mad,
            "p05": _percentile(numeric, 0.05),
            "p95": _percentile(numeric, 0.95),
            "robust_spike_count": spikes,
            "longest_constant_run": _longest_constant_run(series),
        }
    completeness = numeric_cells / expected_cells if expected_cells else 0.0
    spike_fraction = spike_cells / numeric_cells if numeric_cells else 0.0
    return {
        "method": "median_mad_hampel",
        # Distributional deviations can be legitimate phase changes.  Keep the
        # integrity score tied to parse completeness and report deviations as a
        # separate measurement rather than silently treating them as corruption.
        "quality_score": round(100.0 * completeness, 3),
        "numeric_completeness": round(completeness, 6),
        "robust_spike_fraction": round(spike_fraction, 6),
        "columns": summaries,
    }


def neural_reconstruction_profile(
    rows: list[dict[str, Any]],
    *,
    ignored_columns: set[str] | None = None,
    startup_rows: int = 100,
    max_rows: int = 2500,
    random_state: int = 0,
) -> dict[str, Any]:
    """Fit a small denoising MLP autoencoder and score multivariate deviations.

    This is self-supervised: it detects unusual combinations of sensor values,
    not printer defects.  Calibration points are interleaved across the run so
    ordinary process-phase changes do not masquerade as anomalies; no production
    label is invented from unlabeled logs.
    """
    import numpy as np
    from sklearn.exceptions import ConvergenceWarning
    from sklearn.neural_network import MLPRegressor

    candidate_rows = rows[startup_rows:]
    if len(candidate_rows) < 200:
        return {"status": "insufficient_data", "rows": len(candidate_rows)}
    if len(candidate_rows) > max_rows:
        indices = np.linspace(0, len(candidate_rows) - 1, max_rows, dtype=int)
        candidate_rows = [candidate_rows[int(index)] for index in indices]

    ignored = ignored_columns or set()
    all_columns = sorted({key for row in candidate_rows for key in row} - ignored)
    columns: list[str] = []
    raw_columns: list[list[float]] = []
    for column in all_columns:
        values = [
            float(value) if isinstance(value, int | float) and math.isfinite(float(value)) else np.nan
            for value in (row.get(column) for row in candidate_rows)
        ]
        array = np.asarray(values, dtype=float)
        if np.isfinite(array).mean() < 0.8:
            continue
        finite = array[np.isfinite(array)]
        if len(finite) < 2 or float(np.ptp(finite)) <= 1e-12:
            continue
        columns.append(column)
        raw_columns.append(values)
    if len(columns) < 2:
        return {"status": "insufficient_variable_channels", "columns": columns}

    matrix = np.asarray(raw_columns, dtype=float).T
    # Interleave calibration points across the full run.  A chronological tail
    # would mostly measure a normal phase transition (heat-up -> print -> cool-
    # down), not anomalous sensor combinations.
    calibration_mask = np.arange(len(matrix)) % 5 == 0
    training_mask = ~calibration_mask
    train = matrix[training_mask]
    medians = np.nanmedian(train, axis=0)
    matrix = np.where(np.isfinite(matrix), matrix, medians)
    train = matrix[training_mask]
    scales = 1.4826 * np.median(np.abs(train - medians), axis=0)
    fallback = np.std(train, axis=0)
    scales = np.where(scales > 1e-9, scales, np.where(fallback > 1e-9, fallback, 1.0))
    scaled = np.clip((matrix - medians) / scales, -25.0, 25.0)

    train_scaled = scaled[training_mask]
    row_score = np.max(np.abs(train_scaled), axis=1)
    clean_train = train_scaled[row_score <= 8.0]
    if len(clean_train) < 100:
        clean_train = train_scaled
    rng = np.random.default_rng(random_state)
    noisy_train = clean_train + rng.normal(0.0, 0.03, size=clean_train.shape)
    hidden = max(2, min(12, len(columns) // 2))
    model = MLPRegressor(
        hidden_layer_sizes=(hidden,),
        activation="tanh",
        alpha=0.001,
        batch_size=min(128, len(clean_train)),
        learning_rate_init=0.003,
        max_iter=120,
        early_stopping=True,
        validation_fraction=0.15,
        n_iter_no_change=8,
        random_state=random_state,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        model.fit(noisy_train, clean_train)
    reconstruction = model.predict(scaled)
    errors = np.mean(np.square(reconstruction - scaled), axis=1)
    calibration = errors[calibration_mask]
    val_median = float(np.median(calibration))
    val_mad = float(np.median(np.abs(calibration - val_median)))
    threshold = max(
        val_median + 6.0 * max(1.4826 * val_mad, 1e-9),
        float(np.quantile(calibration, 0.995)),
    )
    anomalous = np.flatnonzero(errors > threshold)
    ranked = anomalous[np.argsort(errors[anomalous])[::-1]][:50]
    quartile_counts = Counter(
        min(4, int(index * 4 / max(len(errors), 1))) + 1 for index in anomalous
    )
    return {
        "status": "ok",
        "method": "denoising_mlp_autoencoder",
        "interpretation": "multivariate_sensor_deviation_not_defect_probability",
        "rows": len(matrix),
        "training_rows": len(clean_train),
        "calibration_rows": int(np.sum(calibration_mask)),
        "columns": columns,
        "hidden_units": hidden,
        "iterations": int(model.n_iter_),
        "validation_threshold": threshold,
        "median_reconstruction_error": float(np.median(errors)),
        "anomaly_count": int(len(anomalous)),
        "anomaly_fraction": round(len(anomalous) / len(errors), 6),
        "anomaly_temporal_quartiles": {
            str(quartile): quartile_counts.get(quartile, 0) for quartile in range(1, 5)
        },
        "top_sample_indices": [int(index + startup_rows) for index in ranked],
        "top_scores": [float(errors[index]) for index in ranked],
    }
