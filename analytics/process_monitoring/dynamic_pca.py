"""Dynamic PCA for correlated, lagged printer telemetry."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _aligned_matrix(series: dict[str, list[Any]]) -> tuple[list[str], np.ndarray, list[int]]:
    names = [name for name, values in series.items() if isinstance(values, list)]
    if len(names) < 2:
        return [], np.empty((0, 0)), []
    length = min(len(series[name]) for name in names)
    rows: list[list[float]] = []
    indices: list[int] = []
    for index in range(length):
        row = [series[name][index] for name in names]
        if all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in row):
            rows.append([float(value) for value in row])
            indices.append(index)
    return names, np.asarray(rows, dtype=float), indices


def _lag_embed(matrix: np.ndarray, lags: int) -> np.ndarray:
    return np.asarray([
        np.concatenate([matrix[index - lag] for lag in range(lags + 1)])
        for index in range(lags, len(matrix))
    ])


def analyze_dynamic_pca(
    series: dict[str, list[Any]],
    *,
    lags: int = 2,
    baseline_fraction: float = 0.4,
    explained_variance: float = 0.9,
    min_points: int = 40,
) -> dict[str, Any]:
    """Detect multivariate deviations with a lag-embedded PCA model.

    The first ``baseline_fraction`` of valid observations is the provisional
    reference.  This is explicit in the result because it is a hypothesis, not
    proof that the beginning of a print was healthy.
    """
    names, matrix, source_indices = _aligned_matrix(series)
    if len(matrix) < min_points or len(names) < 2:
        return {
            "status": "insufficient_data",
            "required_points": min_points,
            "actual_points": int(len(matrix)),
            "required_signals": 2,
            "actual_signals": len(names),
        }
    lags = max(0, min(int(lags), 5))
    embedded = _lag_embed(matrix, lags)
    baseline_size = max(20, int(len(embedded) * baseline_fraction))
    baseline_size = min(baseline_size, len(embedded) - 5)
    if baseline_size < 10:
        return {"status": "insufficient_data", "actual_points": int(len(matrix))}

    baseline = embedded[:baseline_size]
    centre = np.median(baseline, axis=0)
    mad = np.median(np.abs(baseline - centre), axis=0) * 1.4826
    standard = np.std(baseline, axis=0)
    scale = np.where(mad > 1e-9, mad, np.where(standard > 1e-9, standard, 1.0))
    normalized = (embedded - centre) / scale
    model_mean = np.mean(normalized[:baseline_size], axis=0)
    fitted = normalized[:baseline_size] - model_mean

    _, singular, vh = np.linalg.svd(fitted, full_matrices=False)
    eigenvalues = singular**2 / max(1, baseline_size - 1)
    positive = eigenvalues > 1e-12
    eigenvalues = eigenvalues[positive]
    vh = vh[positive]
    if len(eigenvalues) == 0:
        return {"status": "insufficient_variation", "actual_points": int(len(matrix))}
    cumulative = np.cumsum(eigenvalues) / np.sum(eigenvalues)
    components = min(len(eigenvalues), int(np.searchsorted(cumulative, explained_variance) + 1))
    basis = vh[:components]
    centered = normalized - model_mean
    scores = centered @ basis.T
    t_squared = np.sum(scores**2 / np.maximum(eigenvalues[:components], 1e-12), axis=1)
    reconstruction = scores @ basis
    residual = centered - reconstruction
    spe = np.sum(residual**2, axis=1)

    t_limit = float(np.quantile(t_squared[:baseline_size], 0.99))
    spe_limit = float(np.quantile(spe[:baseline_size], 0.99))
    t_limit = max(t_limit, 1e-9)
    spe_limit = max(spe_limit, 1e-9)
    combined = np.maximum(t_squared / t_limit, spe / spe_limit)
    candidate_indices = np.flatnonzero(combined[baseline_size:] > 1.0) + baseline_size
    ranked = sorted(candidate_indices, key=lambda index: combined[index], reverse=True)[:20]

    anomalies = []
    signal_count = len(names)
    for row_index in ranked:
        contribution = residual[row_index] ** 2
        by_signal = [
            float(sum(contribution[lag * signal_count + signal] for lag in range(lags + 1)))
            for signal in range(signal_count)
        ]
        top_signal = names[int(np.argmax(by_signal))]
        anomalies.append({
            "index": source_indices[row_index + lags],
            "score_ratio": round(float(combined[row_index]), 3),
            "hotelling_t2": round(float(t_squared[row_index]), 4),
            "squared_prediction_error": round(float(spe[row_index]), 4),
            "main_contributing_signal": top_signal,
        })

    return {
        "status": "ok",
        "mode": "shadow",
        "method": "Dynamic PCA (lag embedding, robust scaling)",
        "signals": names,
        "points": int(len(matrix)),
        "lags": lags,
        "components": components,
        "explained_variance_ratio": round(float(cumulative[components - 1]), 4),
        "baseline": {
            "assumption_ru": "Первые измерения считаются условно нормальным эталоном",
            "points": baseline_size,
            "fraction": round(baseline_size / len(embedded), 3),
        },
        "limits": {"hotelling_t2": round(t_limit, 4), "spe": round(spe_limit, 4)},
        "anomaly_count": int(len(candidate_indices)),
        "anomalies": anomalies,
    }


__all__ = ["analyze_dynamic_pca"]
