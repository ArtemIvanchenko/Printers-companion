"""Predictive-maintenance forecast from cross-session signal drift.

For each signal, fit a robust (Theil-Sen) trend across the per-session means in
chronological order and project how many more sessions until that mean crosses
the signal's alarm threshold (from signals.yaml). A signal drifting toward a
limit is an early, quantified warning that a component is degrading.

Reuses ``analytics.robust_stats.theil_sen_slope_ci`` (point slope plus its
confidence interval) and ``analytics.thresholds.load_alarm_thresholds``. No
raw-log access.
"""
from __future__ import annotations

from datetime import datetime
from statistics import median
from typing import Any

from analytics.prediction.contract import PredictionResult, PredictionSource
from analytics.robust_stats import theil_sen_slope_ci

# Need at least this many sessions with the signal to trust a trend.
MIN_SESSIONS = 4
# Ignore trivial drift: |slope| must be at least this fraction of |mean| per
# session, otherwise the projection is noise-dominated and meaningless.
MIN_REL_SLOPE = 0.005
# Don't report forecasts further out than this (too speculative to action).
MAX_HORIZON_SESSIONS = 200
MAX_TREND_HISTORY = 30


def _signal_series(sessions: list[dict[str, Any]], signal: str) -> list[float]:
    """Per-session means for one signal, in the given (chronological) order."""
    series: list[float] = []
    for s in sessions:
        st = (s.get("signal_stats") or {}).get(signal) or {}
        mean = st.get("mean")
        if isinstance(mean, (int, float)) and not isinstance(mean, bool):
            series.append(float(mean))
    return series[-MAX_TREND_HISTORY:]


def _session_cadence_days(sessions: list[dict[str, Any]]) -> float | None:
    timestamps = []
    for session in sessions:
        raw = session.get("start_ts")
        if not isinstance(raw, str):
            continue
        try:
            timestamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            continue
    gaps = [
        (later - earlier).total_seconds() / 86400
        for earlier, later in zip(timestamps, timestamps[1:])
        if later > earlier
    ]
    return median(gaps[-20:]) if gaps else None


def _group_for(sessions: list[dict[str, Any]], signal: str) -> str:
    for s in sessions:
        g = (s.get("signal_stats") or {}).get(signal, {}).get("group")
        if g:
            return g
    return ""


def _sessions_to_for_slope(
    current: float, threshold: float, slope: float, *, increasing: bool,
) -> float | None:
    """Sessions to threshold for one candidate slope value.

    ``increasing`` is the POINT estimate's drift direction (alarm_high with a
    positive point slope, or alarm_low with a negative one) — fixed once per
    forecast, not re-derived from ``slope``. A confidence-interval bound can
    have the opposite sign from the point estimate (the trend could plausibly
    be flat or reversing); using ``slope``'s own sign to decide "already past"
    conflated that case with "already past the threshold right now", which
    fabricated a spurious 0-session bound whenever the interval crossed zero.

    "Already past" is a fact about ``current`` vs ``threshold`` alone, so it
    does not depend on which candidate slope is being evaluated. Returns
    ``None`` when this slope never reaches the threshold (zero, or pointed
    the wrong way relative to ``increasing``) — the caller must not treat
    that as "far away", it means "this bound of the interval is open-ended".
    """
    remaining = threshold - current
    if remaining <= 0 if increasing else remaining >= 0:
        return 0.0
    if slope == 0 or ((slope <= 0) if increasing else (slope >= 0)):
        return None
    sessions = remaining / slope
    return sessions if sessions > 0 else None


def forecast_maintenance(
    sessions: list[dict[str, Any]],
    alarm_thresholds: dict[str, dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Project per-signal drift toward alarm thresholds.

    Args:
        sessions: session dicts with ``signal_stats`` (each ``{signal: {mean,...}}``),
            ordered chronologically (oldest first).
        alarm_thresholds: ``{signal: {alarm_high, alarm_low}}``; loaded from the
            profile if omitted.

    Returns: list of forecasts (most urgent first), each
        ``{signal, group, direction, slope_per_session, current_mean,
           threshold, threshold_kind, sessions_to_threshold,
           sessions_to_threshold_interval, recommendation, prediction}``.
        Signals that are stable or drifting away from limits are omitted.
        ``sessions_to_threshold_interval`` (and ``prediction.interval``) is
        ``None`` when the slope's confidence interval could not be estimated
        (too few points) — never a fabricated symmetric spread.
    """
    if alarm_thresholds is None:
        from analytics.thresholds import load_alarm_thresholds
        alarm_thresholds = load_alarm_thresholds()

    # All signals that appear anywhere.
    all_signals: set[str] = set()
    for s in sessions:
        all_signals.update((s.get("signal_stats") or {}).keys())

    forecasts: list[dict[str, Any]] = []
    cadence_days = _session_cadence_days(sessions)
    for signal in sorted(all_signals):
        thr = alarm_thresholds.get(signal)
        if not thr:
            continue
        series = _signal_series(sessions, signal)
        if len(series) < MIN_SESSIONS:
            continue

        slope, low_slope, high_slope = theil_sen_slope_ci(series)
        current = series[-1]
        mean_abs = abs(sum(series) / len(series)) or 1.0
        if abs(slope) < MIN_REL_SLOPE * mean_abs:
            continue  # essentially flat

        # Pick the threshold the signal is drifting toward.
        if slope > 0 and "alarm_high" in thr:
            threshold, kind = thr["alarm_high"], "alarm_high"
        elif slope < 0 and "alarm_low" in thr:
            threshold, kind = thr["alarm_low"], "alarm_low"
        else:
            continue  # drifting away from the only available limit

        remaining = threshold - current
        # If already past the threshold, it's urgent now (0 sessions).
        if (slope > 0 and remaining <= 0) or (slope < 0 and remaining >= 0):
            sessions_to = 0.0
        else:
            sessions_to = remaining / slope
            if sessions_to <= 0 or sessions_to > MAX_HORIZON_SESSIONS:
                continue

        direction = "растёт" if slope > 0 else "падает"
        if sessions_to == 0:
            rec = (f"Сигнал {signal} уже за порогом {kind} ({threshold:g}) — "
                   f"проверьте узел сейчас")
        else:
            rec = (f"Сигнал {signal} {direction} к порогу {kind} ({threshold:g}); "
                   f"≈{sessions_to:.0f} печат(ей) до достижения — запланируйте ТО")

        sessions_to = round(sessions_to, 1)

        # Interval from the slope's own confidence bounds: the fast bound is
        # whichever of low_slope/high_slope is steeper in the drift direction,
        # the slow bound the shallower one. A slow bound that doesn't reach the
        # threshold at all (its sign disagrees with the point estimate) is not
        # "no upper bound" — it means the true trend could plausibly never get
        # there, so it is reported capped at MAX_HORIZON_SESSIONS with a warning
        # rather than silently omitted.
        interval: tuple[float, float] | None = None
        interval_warnings: list[str] = []
        if low_slope is not None and high_slope is not None:
            fast_slope, slow_slope = (high_slope, low_slope) if slope > 0 else (low_slope, high_slope)
            sessions_fast = _sessions_to_for_slope(current, threshold, fast_slope, increasing=slope > 0)
            sessions_slow = _sessions_to_for_slope(current, threshold, slow_slope, increasing=slope > 0)
            if sessions_fast is not None:
                if sessions_slow is None:
                    sessions_slow = float(MAX_HORIZON_SESSIONS)
                    interval_warnings.append(
                        "Верхняя граница интервала не определена статистически (в пределах "
                        "доверительного интервала наклон может смениться) — показан потолок "
                        f"{MAX_HORIZON_SESSIONS} печатей."
                    )
                interval = (round(min(sessions_fast, sessions_slow), 1),
                            round(max(sessions_fast, sessions_slow), 1))
        else:
            interval_warnings.append(
                "Доверительный интервал наклона недоступен (мало точек) — показана только "
                "точечная оценка."
            )

        forecasts.append({
            "signal": signal,
            "group": _group_for(sessions, signal),
            "direction": "increasing" if slope > 0 else "decreasing",
            "slope_per_session": round(slope, 6),
            "current_mean": round(current, 6),
            "threshold": threshold,
            "threshold_kind": kind,
            "sessions_to_threshold": sessions_to,
            "sessions_to_threshold_interval": list(interval) if interval is not None else None,
            # Threshold-based RUL: valid only if the observed trend continues.
            "remaining_useful_life": {
                "method": "threshold_projection",
                "sessions": sessions_to,
                "sessions_interval": list(interval) if interval is not None else None,
                "calendar_days": (
                    round(sessions_to * cadence_days, 1) if cadence_days is not None else None
                ),
                "calendar_days_interval": (
                    [round(value * cadence_days, 1) for value in interval]
                    if cadence_days is not None and interval is not None else None
                ),
                "average_days_between_prints": round(cadence_days, 2) if cadence_days is not None else None,
                "reliability": (
                    "high" if len(series) >= 10 and not interval_warnings
                    else "medium" if len(series) >= 6 and interval is not None
                    else "low"
                ),
                "assumption_ru": "Текущий тренд сохраняется, а режим эксплуатации не меняется",
            },
            "n_sessions": len(series),
            "recommendation": rec,
            "prediction": PredictionResult(
                value=sessions_to,
                unit="печатей",
                source=PredictionSource.MODEL,
                interval=interval,
                sample_size=len(series),
                warnings=(
                    ([f"Прогноз построен на минимально допустимом числе сессий ({MIN_SESSIONS})."]
                     if len(series) == MIN_SESSIONS else [])
                    + interval_warnings
                ),
                explanation=(
                    f"Робастный тренд (Тейл-Сен) по {len(series)} последним значениям сигнала "
                    f"{signal}, спроецированный до порога {kind} ({threshold:g})."
                ),
            ).to_dict(),
        })

    return sorted(forecasts, key=lambda f: f["sessions_to_threshold"])


__all__ = ["forecast_maintenance", "MIN_SESSIONS"]
