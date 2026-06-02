"""Shared robust-statistics helpers for the analytics layer.

Single source of truth for the Theil-Sen slope, which several modules need on
arrays of very different sizes (≈2–50 session means vs ≈330k raw sensor rows).
``scipy.stats.theilslopes`` is O(n²), so large inputs are sub-sampled to a
bounded number of evenly-spaced points before the regression.
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from scipy.stats import theilslopes

_DEFAULT_MAX_POINTS = 2000


def theil_sen_slope(
    values: Sequence[float] | np.ndarray,
    xs: Sequence[float] | np.ndarray | None = None,
    *,
    max_points: int = _DEFAULT_MAX_POINTS,
) -> float:
    """Robust linear trend slope (signal units per x-step).

    Args:
        values: y-values in chronological order.
        xs: matching x-positions; defaults to ``0, 1, 2, …``.
        max_points: cap before regression — inputs larger than this are
            sub-sampled to evenly-spaced points (theilslopes is O(n²), so
            330k points would take hours while 2k points take <0.01s).

    Returns 0.0 for degenerate input (fewer than 2 points) or if the
    regression fails, so callers never have to wrap this in try/except.
    """
    vals = np.asarray(values, dtype=float)
    n = vals.size
    if n < 2:
        return 0.0

    x = np.arange(n, dtype=float) if xs is None else np.asarray(xs, dtype=float)

    if n > max_points:
        idx = np.round(np.linspace(0, n - 1, max_points)).astype(int)
        vals, x = vals[idx], x[idx]

    try:
        return float(theilslopes(vals, x).slope)
    except Exception:
        return 0.0
