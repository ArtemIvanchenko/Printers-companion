"""Process-health analytics derived from decoded M-450-M telemetry.

Three capabilities, all pure functions over the compact telemetry dict produced by
``domain.services.session_overview`` (so they run at ingest and are storage-agnostic):

1. detect_process_anomalies  — oxygen / humidity / temperature excursions.
2. analyze_layer_burn_drift  — per-layer burn-time trend and outlier layers.
3. atmosphere_readiness_score — 0..100 composite of inert-atmosphere quality.

Units of the raw channels are not fully confirmed (see signals.yaml), so detection
is built on *relative* statistics (z-scores, coefficient of variation, trend slope)
which are robust to unknown scaling, plus a few clearly-labelled heuristic levels.
"""
from __future__ import annotations

from statistics import fmean, median, pstdev
from typing import Any


def _clean(values: list[Any]) -> list[float]:
    return [float(v) for v in values if isinstance(v, (int, float))]


def _zscores(values: list[float]) -> list[float]:
    if len(values) < 2:
        return [0.0] * len(values)
    mean = fmean(values)
    sd = pstdev(values)
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
    scale = pstdev(base) if len(base) >= 2 else 0.0
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
    return pstdev(values) / abs(mean)


def detect_process_anomalies(telemetry: dict[str, Any], z_threshold: float = 3.5) -> list[dict[str, Any]]:
    """Flag oxygen, humidity and temperature excursions in the telemetry series.

    Spike detection uses a robust modified z-score (median/MAD), unit-agnostic.
    Returns anomaly dicts: {signal, semantic, severity, kind, value, z_score, detail}.
    """
    anomalies: list[dict[str, Any]] = []

    def _scan(group: str, semantic: str, severity: str):
        for col, raw in (telemetry.get(group) or {}).items():
            values = _clean(raw)
            peak = _robust_spike(values)
            if peak is None:
                continue
            peak_idx, z = peak
            if abs(z) >= z_threshold:
                anomalies.append({
                    "signal": col,
                    "semantic": semantic,
                    "kind": "spike",
                    "severity": severity,
                    "value": round(values[peak_idx], 4),
                    "z_score": round(z, 2),
                    "detail": f"{semantic} '{col}' отклонение {z:+.1f} (макс {max(values):.3g})",
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

    # Trend judged relative to the mean burn time so it is unit-robust.
    rel = (slope / mean_y) if mean_y else 0.0
    if rel > 0.01:
        trend = "rising"
    elif rel < -0.01:
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

    def _stability_factor(group: str) -> float | None:
        series = telemetry.get(group) or {}
        cvs = [_coefficient_of_variation(_clean(v)) for v in series.values() if len(_clean(v)) >= 5]
        if not cvs:
            return None
        cv = sum(cvs) / len(cvs)
        # Map CV (0 = perfectly stable) to 0..1; cv>=0.5 -> 0.
        return max(0.0, 1.0 - cv / 0.5)

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
