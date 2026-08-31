"""Explicit-duration hidden semi-Markov model for operating regimes."""

from __future__ import annotations

import math
from typing import Any

import numpy as np


def _gaussian_logpdf(values: np.ndarray, mean: float, std: float) -> np.ndarray:
    std = max(float(std), 1e-6)
    return -0.5 * ((values - mean) / std) ** 2 - math.log(std * math.sqrt(2.0 * math.pi))


def explicit_duration_viterbi(
    observations: list[float],
    states: list[dict[str, Any]],
) -> dict[str, Any]:
    """Most likely HSMM state path with per-state duration distributions.

    Each state defines ``name``, observation ``mean``/``std`` and duration
    ``min_duration``/``max_duration``/``duration_mean``/``duration_std``.
    """
    data = np.asarray(observations, dtype=float)
    if len(data) == 0 or not states:
        return {"status": "insufficient_data", "segments": [], "state_path": []}
    if not np.all(np.isfinite(data)):
        return {"status": "invalid_data", "segments": [], "state_path": []}
    state_count = len(states)
    emission_prefix = []
    for state in states:
        values = _gaussian_logpdf(data, float(state["mean"]), float(state["std"]))
        emission_prefix.append(np.concatenate(([0.0], np.cumsum(values))))
    score = np.full((len(data) + 1, state_count), -np.inf)
    back: dict[tuple[int, int], tuple[int, int | None]] = {}
    transition_penalty = -math.log(max(1, state_count - 1)) if state_count > 1 else 0.0

    for end in range(1, len(data) + 1):
        for state_index, state in enumerate(states):
            minimum = max(1, int(state.get("min_duration", 1)))
            maximum = min(end, int(state.get("max_duration", len(data))))
            duration_mean = float(state.get("duration_mean", minimum))
            duration_std = max(float(state.get("duration_std", duration_mean / 2 or 1)), 1.0)
            for duration in range(minimum, maximum + 1):
                start = end - duration
                emission = emission_prefix[state_index][end] - emission_prefix[state_index][start]
                duration_score = -0.5 * ((duration - duration_mean) / duration_std) ** 2
                if start == 0:
                    candidate = -math.log(state_count) + emission + duration_score
                    previous_state = None
                else:
                    previous_options = [
                        (score[start, other] + transition_penalty, other)
                        for other in range(state_count) if other != state_index
                    ]
                    if not previous_options:
                        previous_options = [(score[start, state_index], state_index)]
                    previous_score, previous_state = max(previous_options, key=lambda item: item[0])
                    candidate = previous_score + emission + duration_score
                if candidate > score[end, state_index]:
                    score[end, state_index] = candidate
                    back[(end, state_index)] = (start, previous_state)

    final_state = int(np.argmax(score[len(data)]))
    if not math.isfinite(float(score[len(data), final_state])):
        return {"status": "no_valid_path", "segments": [], "state_path": []}
    segments = []
    end, state_index = len(data), final_state
    while end > 0:
        start, previous = back[(end, state_index)]
        segments.append({
            "state": states[state_index]["name"],
            "start_index": start,
            "end_index": end - 1,
            "duration": end - start,
            "mean_observation": round(float(np.mean(data[start:end])), 5),
        })
        end = start
        if previous is None:
            break
        state_index = previous
    segments.reverse()
    path = [segment["state"] for segment in segments for _ in range(segment["duration"])]
    return {
        "status": "ok",
        "log_likelihood": round(float(score[len(data), final_state]), 4),
        "segments": segments,
        "state_path": path,
    }


def segment_layer_regimes(values: list[Any], *, min_points: int = 24) -> dict[str, Any]:
    """Fit transparent robust templates and segment fast/typical/slow layer regimes."""
    data = np.asarray([
        float(value) for value in values
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ])
    if len(data) < min_points:
        return {"status": "insufficient_data", "required_points": min_points, "actual_points": len(data)}
    centre = float(np.median(data))
    scale = float(np.median(np.abs(data - centre))) * 1.4826
    scale = scale if scale > 1e-9 else float(np.std(data))
    if scale <= 1e-9:
        return {"status": "insufficient_variation", "actual_points": len(data)}
    maximum = min(len(data), max(12, len(data) // 2))
    states = [
        {"name": "ускоренный цикл", "mean": centre - 1.5 * scale, "std": scale,
         "min_duration": 2, "max_duration": maximum, "duration_mean": 6, "duration_std": 5},
        {"name": "типичный цикл", "mean": centre, "std": scale,
         "min_duration": 2, "max_duration": len(data), "duration_mean": 20, "duration_std": 15},
        {"name": "замедленный цикл", "mean": centre + 1.5 * scale, "std": scale,
         "min_duration": 2, "max_duration": maximum, "duration_mean": 6, "duration_std": 5},
    ]
    result = explicit_duration_viterbi(data.tolist(), states)
    result.update({
        "mode": "shadow",
        "method": "HSMM with explicit state durations",
        "template_source": "robust statistics of this print",
        "limitation_ru": "Режимы относительны этой печати и требуют проверки по размеченным данным",
    })
    result.pop("state_path", None)  # bulky; segments are sufficient for persisted payload
    return result


__all__ = ["explicit_duration_viterbi", "segment_layer_regimes"]
