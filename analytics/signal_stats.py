"""Per-session signal statistics computed from stored 150-point telemetry series.

Called at ingest time (or on-demand) to populate
``session.context.runtime_payload.group.signal_stats``.
Pure Python, no external dependencies.
"""
from __future__ import annotations

import math
from typing import Any


def _stats(values: list[float]) -> dict[str, float]:
    n = len(values)
    s = sorted(values)
    mean = sum(s) / n
    variance = sum((v - mean) ** 2 for v in s) / n
    std = variance ** 0.5
    # Theil-Sen slope on the ORIGINAL (time-ordered) values.
    # Pairs sampled every `step` positions to stay O(n), not O(n²).
    slopes: list[float] = []
    step = max(1, n // 20)
    for i in range(0, n - step, step):
        dy = values[i + step] - values[i]   # time-ordered, not sorted
        slopes.append(dy / step)
    trend_slope = _median(slopes) if slopes else 0.0
    return {
        "mean":        round(mean, 5),
        "std":         round(std, 5),
        "min":         round(s[0], 5),
        "max":         round(s[-1], 5),
        "p95":         round(_percentile(s, 0.95), 5),
        "p05":         round(_percentile(s, 0.05), 5),
        "n":           n,
        "trend_slope": round(trend_slope, 6),  # rising > 0, falling < 0
    }


def _median(values: list[float]) -> float:
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Nearest-rank percentile on a *pre-sorted* list, p in [0, 1].

    Uses the (n-1) basis so p95 of 20 points lands on the 19th value, not the
    max (``int(0.95 * n)`` == 19 == last index, which conflated p95 with max).
    """
    n = len(sorted_vals)
    idx = int(round(p * (n - 1)))
    return sorted_vals[max(0, min(n - 1, idx))]


def compute_signal_stats(telemetry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return per-signal aggregate statistics from a stored telemetry dict.

    Input is the ``telemetry`` sub-dict stored in session context:
        {"oxygen": {"SO1": [...], "SO2": [...]},
         "temperatures": {"ST3": [...], "ST5": [...]},
         "humidity": {"Flow H": [...]},
         "pressure": {"SP4": [...]}}

    Output:
        {"SO1": {"mean": 0.18, "std": 0.04, ..., "group": "oxygen"},
         "ST5": {"mean": 168.2, ..., "group": "temperatures"}, ...}
    """
    result: dict[str, dict[str, Any]] = {}
    group_map = {
        "oxygen":       "oxygen",
        "temperatures": "temperature",
        "humidity":     "humidity",
        "pressure":     "pressure",
    }
    for telem_key, group_label in group_map.items():
        group_data = telemetry.get(telem_key) or {}
        if not isinstance(group_data, dict):
            continue
        for signal, raw_values in group_data.items():
            clean = [
                float(v) for v in (raw_values or [])
                if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            ]
            if len(clean) < 5:
                continue
            s = _stats(clean)
            s["group"] = group_label
            result[signal] = s
    return result
