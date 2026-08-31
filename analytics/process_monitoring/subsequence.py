"""Exact matrix-profile diagnostics for compact per-layer sequences."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _numeric(values: list[Any]) -> np.ndarray:
    return np.asarray([
        float(value) for value in values
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ])


def analyze_matrix_profile(
    values: list[Any],
    *,
    window: int | None = None,
    max_points: int = 360,
) -> dict[str, Any]:
    """Find recurring motifs and unusual subsequences using a z-normalised self-join."""
    raw = _numeric(values)
    if len(raw) > max_points:
        selected = np.linspace(0, len(raw) - 1, max_points).round().astype(int)
        data = raw[selected]
        index_map = selected
    else:
        data = raw
        index_map = np.arange(len(raw))
    if window is None:
        window = max(6, min(30, len(data) // 8))
    window = int(window)
    if window < 4 or len(data) < max(24, window * 4):
        return {
            "status": "insufficient_data",
            "required_points": max(24, window * 4),
            "actual_points": int(len(data)),
        }

    subsequences = np.lib.stride_tricks.sliding_window_view(data, window)
    means = np.mean(subsequences, axis=1)
    stds = np.std(subsequences, axis=1)
    valid = stds > 1e-12
    normalized = np.zeros_like(subsequences, dtype=float)
    normalized[valid] = (subsequences[valid] - means[valid, None]) / stds[valid, None]
    count = len(subsequences)
    profile = np.full(count, np.inf)
    neighbours = np.full(count, -1, dtype=int)
    exclusion = max(1, window // 2)
    for index in range(count):
        if not valid[index]:
            continue
        distances = np.sqrt(np.mean((normalized - normalized[index]) ** 2, axis=1))
        distances[~valid] = np.inf
        distances[max(0, index - exclusion):min(count, index + exclusion + 1)] = np.inf
        neighbour = int(np.argmin(distances))
        if math.isfinite(float(distances[neighbour])):
            profile[index] = distances[neighbour]
            neighbours[index] = neighbour

    finite = np.flatnonzero(np.isfinite(profile))
    if len(finite) < 3:
        return {"status": "insufficient_variation", "actual_points": int(len(data))}
    motif_index = int(finite[np.argmin(profile[finite])])
    discord_index = int(finite[np.argmax(profile[finite])])
    finite_profile = profile[finite]
    profile_median = float(np.median(finite_profile))
    profile_mad = float(np.median(np.abs(finite_profile - profile_median)))
    threshold = profile_median + 3.5 * 1.4826 * profile_mad

    return {
        "status": "ok",
        "mode": "shadow",
        "method": "Matrix Profile (exact z-normalized self-join)",
        "points": int(len(data)),
        "original_points": int(len(raw)),
        "downsampled": len(data) != len(raw),
        "window": window,
        "motif": {
            "start_index": int(index_map[motif_index]),
            "matching_start_index": int(index_map[neighbours[motif_index]]),
            "distance": round(float(profile[motif_index]), 4),
        },
        "discord": {
            "start_index": int(index_map[discord_index]),
            "distance": round(float(profile[discord_index]), 4),
            "robust_threshold": round(float(threshold), 4),
            "is_anomalous": bool(profile_mad > 0 and profile[discord_index] > threshold),
        },
        "limitation_ru": "Индекс относится к началу окна; при сжатии ряда он приблизительный",
    }


__all__ = ["analyze_matrix_profile"]
