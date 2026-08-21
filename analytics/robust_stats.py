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


def theil_sen_slope_ci(
    values: Sequence[float] | np.ndarray,
    xs: Sequence[float] | np.ndarray | None = None,
    *,
    max_points: int = _DEFAULT_MAX_POINTS,
    alpha: float = 0.95,
) -> tuple[float, float | None, float | None]:
    """Theil-Sen slope with a confidence interval on the slope itself.

    Returns ``(slope, low_slope, high_slope)`` at the given confidence level.
    The bounds are ``None`` — not a fabricated zero-width interval — whenever
    scipy cannot estimate them (too few points, degenerate input): callers
    must treat that as "no interval available", not as "the slope is exact".
    """
    vals = np.asarray(values, dtype=float)
    n = vals.size
    if n < 2:
        return 0.0, None, None

    x = np.arange(n, dtype=float) if xs is None else np.asarray(xs, dtype=float)
    if n > max_points:
        idx = np.round(np.linspace(0, n - 1, max_points)).astype(int)
        vals, x = vals[idx], x[idx]

    try:
        result = theilslopes(vals, x, alpha=alpha)
        low, high = float(result.low_slope), float(result.high_slope)
        if not (np.isfinite(low) and np.isfinite(high)):
            return float(result.slope), None, None
        return float(result.slope), low, high
    except Exception:
        return 0.0, None, None
