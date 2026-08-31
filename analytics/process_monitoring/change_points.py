"""Bayesian online change-point detection for compact telemetry."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.special import gammaln


def _student_logpdf(value: float, mu: np.ndarray, kappa: np.ndarray,
                    alpha: np.ndarray, beta: np.ndarray) -> np.ndarray:
    degrees = 2.0 * alpha
    scale2 = beta * (kappa + 1.0) / (alpha * kappa)
    scale2 = np.maximum(scale2, 1e-12)
    z2 = (value - mu) ** 2 / scale2
    return (
        gammaln((degrees + 1.0) / 2.0) - gammaln(degrees / 2.0)
        - 0.5 * np.log(degrees * math.pi * scale2)
        - ((degrees + 1.0) / 2.0) * np.log1p(z2 / degrees)
    )


def detect_bayesian_change_points(
    values: list[Any],
    *,
    expected_run_length: int = 80,
    min_points: int = 24,
) -> dict[str, Any]:
    """Run conjugate Gaussian BOCPD and report the strongest posterior resets."""
    data = np.asarray([
        float(value) for value in values
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    ])
    if len(data) < min_points or float(np.std(data)) <= 1e-12:
        return {
            "status": "insufficient_data" if len(data) < min_points else "insufficient_variation",
            "required_points": min_points,
            "actual_points": int(len(data)),
        }

    hazard = 1.0 / max(10, int(expected_run_length))
    initial_mean = float(np.median(data[:max(5, len(data) // 10)]))
    initial_scale = float(np.std(data[:max(10, len(data) // 5)]))
    if initial_scale <= 1e-9:
        initial_scale = float(np.std(data)) or 1.0
    prior_mu, prior_kappa, prior_alpha = initial_mean, 0.25, 2.0
    prior_beta = max(initial_scale**2, 1e-9)

    probabilities = np.asarray([1.0])
    mu = np.asarray([prior_mu])
    kappa = np.asarray([prior_kappa])
    alpha = np.asarray([prior_alpha])
    beta = np.asarray([prior_beta])
    cp_probabilities: list[float] = []
    map_runs: list[int] = []

    for value in data:
        growth_log = np.log(probabilities + 1e-300) + math.log1p(-hazard)
        growth_log += _student_logpdf(float(value), mu, kappa, alpha, beta)
        prior_log = float(_student_logpdf(
            float(value), np.asarray([prior_mu]), np.asarray([prior_kappa]),
            np.asarray([prior_alpha]), np.asarray([prior_beta]),
        )[0])
        change_log = math.log(hazard) + prior_log
        max_log = max(change_log, float(np.max(growth_log)))
        new_probabilities = np.empty(len(probabilities) + 1)
        new_probabilities[0] = math.exp(change_log - max_log)
        new_probabilities[1:] = np.exp(growth_log - max_log)
        new_probabilities /= np.sum(new_probabilities)

        updated_kappa = kappa + 1.0
        updated_mu = (kappa * mu + value) / updated_kappa
        updated_alpha = alpha + 0.5
        updated_beta = beta + kappa * (value - mu) ** 2 / (2.0 * updated_kappa)
        mu = np.concatenate(([prior_mu], updated_mu))
        kappa = np.concatenate(([prior_kappa], updated_kappa))
        alpha = np.concatenate(([prior_alpha], updated_alpha))
        beta = np.concatenate(([prior_beta], updated_beta))
        probabilities = new_probabilities
        cp_probabilities.append(float(probabilities[0]))
        map_runs.append(int(np.argmax(probabilities)))

    scores = np.asarray(cp_probabilities)
    candidates = sorted(range(3, len(data)), key=lambda index: scores[index], reverse=True)
    selected: list[int] = []
    spacing = max(2, min(10, len(data) // 20))
    for index in candidates:
        run_reset = map_runs[index] < max(3, map_runs[index - 1] // 3)
        if scores[index] < max(hazard * 2.0, 0.02) and not run_reset:
            continue
        if all(abs(index - existing) >= spacing for existing in selected):
            selected.append(index)
        if len(selected) == 10:
            break

    return {
        "status": "ok",
        "mode": "shadow",
        "method": "Bayesian online change-point detection",
        "points": int(len(data)),
        "expected_run_length": int(expected_run_length),
        "hazard": round(hazard, 6),
        "change_points": [
            {
                "index": index,
                "posterior_probability": round(cp_probabilities[index], 5),
                "map_run_length": map_runs[index],
            }
            for index in sorted(selected)
        ],
        "strongest_index": int(np.argmax(scores)),
        "strongest_probability": round(float(np.max(scores)), 5),
        "limitation_ru": "Вероятность зависит от заданной средней длины стабильного режима",
    }


__all__ = ["detect_bayesian_change_points"]
