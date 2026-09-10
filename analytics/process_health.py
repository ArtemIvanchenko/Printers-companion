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
from datetime import datetime
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
    from analytics.thresholds import (
        load_valid_ranges,
        should_apply_valid_range,
        value_in_valid_range,
        value_is_explicitly_invalid,
    )

    rng = load_valid_ranges().get(signal) or {}
    if not rng:
        return cleaned
    cleaned = [value for value in cleaned if not value_is_explicitly_invalid(value, rng)]
    if not cleaned:
        return []
    accepted = [value for value in cleaned if value_in_valid_range(value, rng)]
    # Candidate profile mappings can have wrong units. Match the full-stats
    # parser: confirmed ranges stay active while contradictory candidate ranges
    # are ignored instead of erasing data.
    rejected_fraction = 1.0 - len(accepted) / len(cleaned)
    return accepted if should_apply_valid_range(rng, rejected_fraction) else cleaned


def _clean_signal_indexed(signal: str, values: list[Any]) -> list[tuple[int, float]]:
    """The same physical filtering as :func:`_clean_signal`, retaining row ids.

    An anomaly without its source row cannot later be associated with print
    progress, layer height or an STL region.  The legacy helper remains intact
    for callers that only need values; this indexed variant is used by anomaly
    detection and adds provenance without changing any existing result keys.
    """
    candidates = [
        (index, float(value))
        for index, value in enumerate(values)
        if isinstance(value, (int, float))
        and math.isfinite(float(value))
        and abs(float(value)) <= 1e7
    ]
    if not candidates:
        return []
    from analytics.thresholds import (
        load_valid_ranges,
        should_apply_valid_range,
        value_in_valid_range,
        value_is_explicitly_invalid,
    )

    rng = load_valid_ranges().get(signal) or {}
    if not rng:
        return candidates
    candidates = [pair for pair in candidates if not value_is_explicitly_invalid(pair[1], rng)]
    if not candidates:
        return []
    accepted = [pair for pair in candidates if value_in_valid_range(pair[1], rng)]
    rejected_fraction = 1.0 - len(accepted) / len(candidates)
    return accepted if should_apply_valid_range(rng, rejected_fraction) else candidates


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
    """Flag oxygen, pressure, humidity and temperature telemetry excursions.

    Spike detection uses a robust modified z-score (median/MAD), unit-agnostic.
    Returns anomaly dicts: {signal, semantic, severity, kind, value, z_score, detail}.
    """
    anomalies: list[dict[str, Any]] = []
    from analytics.thresholds import load_alarm_thresholds

    thresholds = load_alarm_thresholds()
    time_axis = telemetry.get("time") or []
    timestamp_axis = telemetry.get("timestamps") or []
    layer_time_points: list[tuple[datetime, int]] = []
    for point in telemetry.get("layer_time_points") or []:
        if not isinstance(point, dict) or not isinstance(point.get("layer"), int):
            continue
        try:
            layer_time_points.append((datetime.fromisoformat(point["timestamp"]), point["layer"]))
        except (KeyError, TypeError, ValueError):
            continue
    layer_time_points.sort()
    layer_steps = [
        (layer_time_points[index][0] - layer_time_points[index - 1][0]).total_seconds()
        for index in range(1, len(layer_time_points))
        if layer_time_points[index][0] > layer_time_points[index - 1][0]
    ]
    typical_layer_step = median(layer_steps) if layer_steps else None

    def _layer_location(timestamp: Any) -> dict[str, Any]:
        if not isinstance(timestamp, str) or not layer_time_points:
            return {}
        try:
            moment = datetime.fromisoformat(timestamp)
        except ValueError:
            return {}
        nearest_time, nearest_layer = min(
            layer_time_points, key=lambda point: abs((point[0] - moment).total_seconds())
        )
        distance = abs((nearest_time - moment).total_seconds())
        tolerance = max(300.0, (typical_layer_step or 0.0) * 2.5)
        if distance <= tolerance:
            return {
                "layer": nearest_layer,
                "layer_mapping_precision": "nearest_burn_log_timestamp",
                "layer_time_distance_sec": round(distance, 1),
            }
        before = [layer for ts, layer in layer_time_points if ts <= moment]
        after = [layer for ts, layer in layer_time_points if ts >= moment]
        if before and after:
            return {
                "layer_range": [min(before[-1], after[0]), max(before[-1], after[0])],
                "layer_mapping_precision": "timestamp_gap_range",
                "layer_time_distance_sec": round(distance, 1),
            }
        return {}

    def _location(source_index: int, sample_count: int) -> dict[str, Any]:
        result: dict[str, Any] = {
            "sample_index": source_index,
            "sample_count": sample_count,
        }
        if source_index < len(time_axis) and time_axis[source_index] is not None:
            result["time"] = time_axis[source_index]
        if source_index < len(timestamp_axis) and timestamp_axis[source_index] is not None:
            result["timestamp"] = timestamp_axis[source_index]
            result.update(_layer_location(timestamp_axis[source_index]))
        return result

    def _alarm_ranges(samples: list[tuple[int, float]], sample_count: int) -> list[dict[str, Any]]:
        """Contiguous source ranges; a long alarm is not one point anomaly."""
        if not samples:
            return []
        ranges: list[dict[str, Any]] = []
        start = previous = samples[0][0]
        for index, _ in samples[1:]:
            if index > previous + 1:
                ranges.append({
                    "start": _location(start, sample_count),
                    "end": _location(previous, sample_count),
                })
                start = index
            previous = index
        ranges.append({
            "start": _location(start, sample_count),
            "end": _location(previous, sample_count),
        })
        return ranges[:20]

    def _scan(group: str, semantic: str, severity: str):
        for col, raw in (telemetry.get(group) or {}).items():
            indexed = _clean_signal_indexed(col, raw)
            values = [value for _, value in indexed]
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
                    **_location(indexed[peak_idx][0], len(raw)),
                })

            thr = thresholds.get(col) or {}
            high = thr.get("alarm_high")
            low = thr.get("alarm_low")
            directional_alarms = [
                ("выше", high, [pair for pair in indexed if high is not None and pair[1] > high]),
                ("ниже", low, [pair for pair in indexed if low is not None and pair[1] < low]),
            ]
            for direction, boundary, alarm_samples in directional_alarms:
                if not alarm_samples or boundary is None:
                    continue
                alarm_values = [value for _, value in alarm_samples]
                located = (
                    max(alarm_samples, key=lambda pair: pair[1])
                    if direction == "выше" else
                    min(alarm_samples, key=lambda pair: pair[1])
                )
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
                    "sample_ranges": _alarm_ranges(alarm_samples, len(raw)),
                    **_location(located[0], len(raw)),
                })

    # Oxygen excursions are the most safety-relevant for metal AM (oxidation).
    _scan("oxygen", "кислород", "high")
    _scan("pressure", "давление", "medium")
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
