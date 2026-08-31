"""Process-health analytics derived from decoded M-450-M telemetry.

Three capabilities, all pure functions over the compact telemetry dict produced by
``domain.services.session_overview`` (so they run at ingest and are storage-agnostic):

1. detect_process_anomalies  — oxygen / humidity / temperature excursions.
2. analyze_layer_burn_drift  — per-layer burn-time trend and outlier layers.
3. atmosphere_readiness_score — 0..100 composite of inert-atmosphere quality.

Relative statistics detect changes, while passport alarm thresholds detect a
stable but unsafe absolute level. Both are required: perfectly stable
atmospheric oxygen is not a healthy inert chamber.
"""
from __future__ import annotations

import math
from statistics import fmean, median
from typing import Any


def _clean(values: list[Any]) -> list[float]:
    """Return finite, non-firmware-garbage floats."""
    result = []
    for v in values:
        if not isinstance(v, (int, float)):
            continue
        f = float(v)
        if math.isfinite(f) and abs(f) <= 1e7:
            result.append(f)
    return result


def _clean_signal(signal: str, values: list[Any]) -> list[float]:
    """Apply physical bounds when the profile agrees with the sampled data."""
    cleaned = _clean(values)
    if not cleaned:
        return []
    from analytics.thresholds import load_valid_ranges

    rng = load_valid_ranges().get(signal) or {}
    if not rng:
        return cleaned
    accepted = [
        value for value in cleaned
        if (rng.get("min_val") is None or value >= rng["min_val"])
        and (rng.get("max_val") is None or value <= rng["max_val"])
    ]
    # Candidate profile mappings can have wrong units. Match the full-stats
    # parser: ignore a range that rejects more than 20% instead of erasing data.
    return accepted if len(accepted) >= 0.8 * len(cleaned) else cleaned


def _pstdev(values: list[float]) -> float:
    """Population std-dev using plain math — avoids statistics.pstdev
    which breaks on very large floats in Python 3.11 (AttributeError on Fraction)."""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(variance)


def _zscores(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    mean = fmean(values)
    sd = _pstdev(values)
    if sd == 0:
        return [0.0] * len(values)
    return [(v - mean) / sd for v in values]


def _robust_spike(values: list[float]) -> tuple[int, float] | None:
    """Find the most extreme point via a median/MAD modified z-score.

    Robust because the outlier itself does not inflate the scale estimate (unlike a
    plain z-score, which on small samples caps how large any single z can be). When
    the baseline is perfectly flat (MAD == 0) any differing point is treated as a
    clear spike.
    """
    n = len(values)
    if n < 5:
        return None
    med = median(values)
    peak_idx = max(range(n), key=lambda i: abs(values[i] - med))
    mad = median([abs(v - med) for v in values])
    if mad > 0:
        z = 0.6745 * (values[peak_idx] - med) / mad  # modified z-score
        return (peak_idx, z)
    # Flat baseline: estimate scale from all-but-the-most-extreme point.
    base = sorted(values, key=lambda v: abs(v - med))[:-1]
    scale = _pstdev(base) if len(base) >= 2 else 0.0
    if scale > 0:
        return (peak_idx, (values[peak_idx] - med) / scale)
    # Truly constant baseline with a single different value.
    if values[peak_idx] != med:
        return (peak_idx, 99.0)
    return None


def _coefficient_of_variation(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = fmean(values)
    if mean == 0:
        return 0.0
    return _pstdev(values) / abs(mean)


def detect_process_anomalies(telemetry: dict[str, Any], z_threshold: float = 3.5) -> list[dict[str, Any]]:
    """Flag oxygen, humidity and temperature excursions in the telemetry series.

    Spike detection uses a robust modified z-score (median/MAD), unit-agnostic.
    Returns anomaly dicts: {signal, semantic, severity, kind, value, z_score, detail}.
    """
    anomalies: list[dict[str, Any]] = []
    from analytics.thresholds import load_alarm_thresholds

    thresholds = load_alarm_thresholds()

    def _scan(group: str, semantic: str, severity: str):
        for col, raw in (telemetry.get(group) or {}).items():
            values = _clean_signal(col, raw)
            if not values:
                continue
            peak = _robust_spike(values)
            if peak is not None:
                peak_idx, z = peak
            else:
                peak_idx, z = 0, 0.0
            if peak is not None and abs(z) >= z_threshold:
                anomalies.append({
                    "signal": col,
                    "semantic": semantic,
                    "kind": "spike",
                    "severity": severity,
                    "value": round(values[peak_idx], 4),
                    "z_score": round(z, 2),
                    "detail": f"{semantic} '{col}' отклонение {z:+.1f} (макс {max(values):.3g})",
                })

            thr = thresholds.get(col) or {}
            high = thr.get("alarm_high")
            low = thr.get("alarm_low")
            alarm_values = [
                value for value in values
                if (high is not None and value > high) or (low is not None and value < low)
            ]
            if alarm_values:
                direction = "выше" if high is not None and max(alarm_values) > high else "ниже"
                boundary = high if direction == "выше" else low
                anomalies.append({
                    "signal": col,
                    "semantic": semantic,
                    "kind": "threshold",
                    "severity": severity,
                    "value": round(max(alarm_values) if direction == "выше" else min(alarm_values), 4),
                    "alarm_fraction": round(len(alarm_values) / len(values), 3),
                    "detail": (
                        f"{semantic} '{col}': {len(alarm_values)}/{len(values)} измерений "
                        f"{direction} порога {boundary:g}"
                    ),
                })

    # Oxygen excursions are the most safety-relevant for metal AM (oxidation).
    _scan("oxygen", "кислород", "high")
    _scan("humidity", "влажность", "medium")
    _scan("temperatures", "температура", "medium")
    return anomalies


def analyze_layer_burn_drift(layer_burn_times: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect upward drift and outlier layers in per-layer burn duration.

    Returns {n_layers, mean_sec, slope_sec_per_layer, trend, outlier_layers}.
    Rising burn time across layers is an early indicator of process degradation.
    """
    points = [
        (p["layer"], float(p["duration_sec"]))
        for p in layer_burn_times
        if isinstance(p.get("duration_sec"), (int, float)) and p["duration_sec"] > 0
    ]
    if len(points) < 3:
        return {"n_layers": len(points), "trend": "insufficient_data",
                "mean_sec": None, "slope_sec_per_layer": None, "outlier_layers": []}

    xs = [float(layer) for layer, _ in points]
    ys = [dur for _, dur in points]
    mean_x, mean_y = fmean(xs), fmean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom if denom else 0.0

    # Judge the estimated change over the observed layer span. Comparing a
    # per-layer slope directly with the mean made the same 10→20 s drift look
    # weaker merely because a build had more layers.
    rel_change = slope * (max(xs) - min(xs)) / mean_y if mean_y else 0.0
    if rel_change > 0.05:
        trend = "rising"
    elif rel_change < -0.05:
        trend = "falling"
    else:
        trend = "stable"

    zs = _zscores(ys)
    outliers = [
        {"layer": layer, "duration_sec": round(dur, 1), "z_score": round(z, 2)}
        for (layer, dur), z in zip(points, zs)
        if abs(z) >= 3.0
    ]
    return {
        "n_layers": len(points),
        "mean_sec": round(mean_y, 1),
        "slope_sec_per_layer": round(slope, 4),
        "relative_change_pct": round(rel_change * 100, 1),
        "trend": trend,
        "outlier_layers": outliers[:20],
    }


def atmosphere_readiness_score(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Composite 0..100 score for inert-atmosphere quality during the print.

    Heuristic. Built from three stability factors (higher = better):
      - oxygen stability   (low coefficient of variation; oxidation control)
      - humidity stability (low mean & variation)
      - pressure stability (low coefficient of variation; sealed chamber)
    Each factor contributes up to its weight; missing channels are skipped and the
    score is renormalised over available factors.
    """
    factors: dict[str, float] = {}

    from analytics.thresholds import load_alarm_thresholds

    thresholds = load_alarm_thresholds()

    def _stability_factor(group: str) -> float | None:
        series = telemetry.get(group) or {}
        channel_factors: list[float] = []
        for signal, raw in series.items():
            values = _clean_signal(signal, raw)
            if len(values) < 5:
                continue
            cv = _coefficient_of_variation(values)
            stability = max(0.0, 1.0 - cv / 0.5)
            thr = thresholds.get(signal) or {}
            high, low = thr.get("alarm_high"), thr.get("alarm_low")
            if high is None and low is None:
                channel_factors.append(stability)
                continue
            safe_fraction = sum(
                1 for value in values
                if (high is None or value <= high) and (low is None or value >= low)
            ) / len(values)
            # Absolute safety dominates; stability remains useful inside the
            # safe range and prevents noisy near-limit data scoring perfectly.
            channel_factors.append(0.35 * stability + 0.65 * safe_fraction)
        if not channel_factors:
            return None
        return sum(channel_factors) / len(channel_factors)

    weights = {"oxygen": 0.5, "pressure": 0.3, "humidity": 0.2}
    for group, weight in weights.items():
        f = _stability_factor(group)
        if f is not None:
            factors[group] = round(f, 3)

    if not factors:
        return {"score": None, "grade": "unknown", "factors": {}}

    total_weight = sum(weights[g] for g in factors)
    score = sum(factors[g] * weights[g] for g in factors) / total_weight * 100
    score = round(score, 1)
    grade = "good" if score >= 75 else "fair" if score >= 50 else "poor"
    return {"score": score, "grade": grade, "factors": factors}


def build_process_health(telemetry: dict[str, Any]) -> dict[str, Any]:
    """Convenience bundle of all three analyses for a session's telemetry."""
    if not telemetry:
        return {"anomalies": [], "burn_drift": {"trend": "insufficient_data"}, "readiness": {"score": None}}
    return {
        "anomalies": detect_process_anomalies(telemetry),
        "burn_drift": analyze_layer_burn_drift(telemetry.get("layer_burn_times", [])),
        "readiness": atmosphere_readiness_score(telemetry),
    }


__all__ = [
    "detect_process_anomalies",
    "analyze_layer_burn_drift",
    "atmosphere_readiness_score",
    "build_process_health",
]
