"""Sequence comparison with constrained Dynamic Time Warping."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _standardize(values: list[Any]) -> np.ndarray:
    data = np.asarray([
        float(value) for value in values
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ])
    if not len(data):
        return data
    median = float(np.median(data))
    mad = float(np.median(np.abs(data - median))) * 1.4826
    scale = mad if mad > 1e-9 else float(np.std(data))
    return (data - median) / (scale if scale > 1e-9 else 1.0)


def dynamic_time_warping(
    reference: list[Any],
    candidate: list[Any],
    *,
    window_fraction: float = 0.15,
) -> dict[str, Any]:
    """Compare different-length layer sequences within a Sakoe-Chiba band."""
    left, right = _standardize(reference), _standardize(candidate)
    if len(left) < 5 or len(right) < 5:
        return {"status": "insufficient_data", "reference_points": len(left), "candidate_points": len(right)}
    band = max(abs(len(left) - len(right)), int(max(len(left), len(right)) * window_fraction), 1)
    costs = np.full((len(left) + 1, len(right) + 1), np.inf)
    costs[0, 0] = 0.0
    predecessor: dict[tuple[int, int], tuple[int, int]] = {}
    for i in range(1, len(left) + 1):
        for j in range(max(1, i - band), min(len(right), i + band) + 1):
            options = ((costs[i - 1, j], (i - 1, j)),
                       (costs[i, j - 1], (i, j - 1)),
                       (costs[i - 1, j - 1], (i - 1, j - 1)))
            previous_cost, previous = min(options, key=lambda item: item[0])
            costs[i, j] = abs(left[i - 1] - right[j - 1]) + previous_cost
            predecessor[(i, j)] = previous
    if not math.isfinite(float(costs[-1, -1])):
        return {"status": "no_alignment", "window": band}
    path = []
    cursor = (len(left), len(right))
    while cursor != (0, 0):
        path.append((cursor[0] - 1, cursor[1] - 1))
        cursor = predecessor[cursor]
    path.reverse()
    normalized_cost = float(costs[-1, -1]) / len(path)
    diagonal_length = max(len(left), len(right))
    return {
        "status": "ok",
        "mode": "shadow",
        "method": "Dynamic Time Warping (robust scaling, Sakoe-Chiba band)",
        "reference_points": int(len(left)),
        "candidate_points": int(len(right)),
        "window": band,
        "distance": round(normalized_cost, 5),
        "path_length": len(path),
        "warping_ratio": round(len(path) / diagonal_length, 4),
        "path_preview": [{"reference": i, "candidate": j} for i, j in path[::max(1, len(path) // 25)]],
    }


def compare_similar_layer_sequences(
    sessions: list[dict[str, Any]],
    *,
    maximum_layer_difference: float = 0.15,
    max_pairs: int = 12,
) -> list[dict[str, Any]]:
    """Compare each print with the nearest earlier print of similar layer count.

    Layer count is only a weak geometry proxy, so results remain explicitly in
    shadow mode and carry this limitation in every comparison.
    """
    usable = [
        session for session in sessions
        if len(session.get("layer_burn_values") or []) >= 5
    ]
    comparisons: list[dict[str, Any]] = []
    for index in range(1, len(usable)):
        candidate = usable[index]
        candidate_values = candidate["layer_burn_values"]
        choices = []
        for previous in usable[:index]:
            previous_values = previous["layer_burn_values"]
            relative = abs(len(previous_values) - len(candidate_values)) / max(
                len(previous_values), len(candidate_values)
            )
            if relative <= maximum_layer_difference:
                choices.append((relative, previous))
        if not choices:
            continue
        _, reference = min(choices, key=lambda item: item[0])
        alignment = dynamic_time_warping(reference["layer_burn_values"], candidate_values)
        if alignment.get("status") != "ok":
            continue
        comparisons.append({
            "reference_session_id": reference["session_id"],
            "candidate_session_id": candidate["session_id"],
            "reference_layers": len(reference["layer_burn_values"]),
            "candidate_layers": len(candidate_values),
            **alignment,
            "limitation_ru": (
                "Близкое число слоёв не гарантирует одинаковую геометрию; "
                "сравнение диагностическое"
            ),
        })
    return comparisons[-max_pairs:]


__all__ = ["compare_similar_layer_sequences", "dynamic_time_warping"]
