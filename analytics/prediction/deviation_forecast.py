"""One-session-ahead signal forecasts with empirical uncertainty."""

from __future__ import annotations

import math
from statistics import median
from typing import Any

from analytics.prediction.contract import PredictionResult, PredictionSource
from analytics.robust_stats import theil_sen_slope

MIN_SESSIONS = 6
MAX_HISTORY = 30


def _series(sessions: list[dict[str, Any]], signal: str) -> list[float]:
    values = []
    for session in sessions:
        value = ((session.get("signal_stats") or {}).get(signal) or {}).get("mean")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values.append(float(value))
    return values[-MAX_HISTORY:]


def _conformal_error(values: list[float]) -> tuple[float | None, int]:
    """90% rolling-origin absolute-error quantile without future leakage."""
    errors = []
    for end in range(4, len(values)):
        history = values[:end]
        slope = theil_sen_slope(history)
        errors.append(abs(values[end] - (history[-1] + slope)))
    if len(errors) < 3:
        return None, len(errors)
    ordered = sorted(errors)
    # Finite-sample split-conformal rank, clipped to the available sample.
    rank = min(len(ordered) - 1, math.ceil(0.9 * (len(ordered) + 1)) - 1)
    return ordered[max(0, rank)], len(errors)


def _robust_z(value: float, history: list[float]) -> float | None:
    centre = median(history)
    mad = median(abs(item - centre) for item in history)
    if mad <= 1e-12:
        return None if value == centre else math.copysign(99.0, value - centre)
    return 0.6745 * (value - centre) / mad


def forecast_signal_deviations(
    sessions: list[dict[str, Any]],
    alarm_thresholds: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Forecast the next session mean and flag drift/deviation/threshold risk.

    The interval is a rolling-origin conformal band based only on earlier
    prediction errors.  It remains ``None`` until at least three honest
    out-of-time errors exist.
    """
    if alarm_thresholds is None:
        from analytics.thresholds import load_alarm_thresholds
        alarm_thresholds = load_alarm_thresholds()
    signals = sorted({
        signal for session in sessions for signal in (session.get("signal_stats") or {})
    })
    forecasts = []
    for signal in signals:
        values = _series(sessions, signal)
        if len(values) < MIN_SESSIONS:
            continue
        slope = theil_sen_slope(values)
        predicted = values[-1] + slope
        error, error_count = _conformal_error(values)
        interval = (predicted - error, predicted + error) if error is not None else None
        robust_z = _robust_z(predicted, values)
        threshold = alarm_thresholds.get(signal) or {}
        high, low = threshold.get("alarm_high"), threshold.get("alarm_low")
        threshold_risk = (
            (high is not None and ((interval or (predicted, predicted))[1] >= high))
            or (low is not None and ((interval or (predicted, predicted))[0] <= low))
        )
        centre = abs(sum(values) / len(values)) or 1.0
        material_drift = abs(slope) >= 0.005 * centre
        if threshold_risk:
            status = "threshold_risk"
            status_ru = "Риск достижения паспортного порога"
        elif robust_z is not None and abs(robust_z) >= 3.5:
            status = "deviation_expected"
            status_ru = "Ожидается нетипичное значение"
        elif material_drift:
            status = "drift"
            status_ru = "Наблюдается направленный дрейф"
        else:
            status = "normal"
            status_ru = "Значимое отклонение не ожидается"
        warning = []
        if interval is None:
            warning.append("Недостаточно последовательных ошибок для эмпирического интервала.")
        forecasts.append({
            "signal": signal,
            "status": status,
            "status_ru": status_ru,
            "current_mean": round(values[-1], 6),
            "predicted_next_mean": round(predicted, 6),
            "trend_per_session": round(slope, 6),
            "trend_direction": "increasing" if slope > 0 else "decreasing" if slope < 0 else "stable",
            "robust_deviation_score": round(robust_z, 3) if robust_z is not None else None,
            "prediction": PredictionResult(
                value=round(predicted, 6),
                unit="среднее значение сигнала в следующей печати",
                source=PredictionSource.MODEL,
                interval=(round(interval[0], 6), round(interval[1], 6)) if interval else None,
                sample_size=len(values),
                warnings=warning,
                explanation=(
                    "Прогноз Тейла–Сена на один шаг; диапазон получен из "
                    f"{error_count} последовательных ошибок backtest без перемешивания времени."
                ),
            ).to_dict(),
        })
    priority = {"threshold_risk": 0, "deviation_expected": 1, "drift": 2, "normal": 3}
    return sorted(forecasts, key=lambda item: (priority[item["status"]], item["signal"]))


__all__ = ["forecast_signal_deviations", "MIN_SESSIONS"]
