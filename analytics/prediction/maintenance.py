"""Predictive-maintenance forecast from cross-session signal drift.

For each signal, fit a robust (Theil-Sen) trend across the per-session means in
chronological order and project how many more sessions until that mean crosses
the signal's alarm threshold (from signals.yaml). A signal drifting toward a
limit is an early, quantified warning that a component is degrading.

Reuses ``analytics.robust_stats.theil_sen_slope`` and
``analytics.thresholds.load_alarm_thresholds``. No raw-log access.
"""
from __future__ import annotations

from typing import Any

from analytics.robust_stats import theil_sen_slope

# Need at least this many sessions with the signal to trust a trend.
MIN_SESSIONS = 4
# Ignore trivial drift: |slope| must be at least this fraction of |mean| per
# session, otherwise the projection is noise-dominated and meaningless.
MIN_REL_SLOPE = 0.005
# Don't report forecasts further out than this (too speculative to action).
MAX_HORIZON_SESSIONS = 200


def _signal_series(sessions: list[dict[str, Any]], signal: str) -> list[float]:
    """Per-session means for one signal, in the given (chronological) order."""
    series: list[float] = []
    for s in sessions:
        st = (s.get("signal_stats") or {}).get(signal) or {}
        mean = st.get("mean")
        if isinstance(mean, (int, float)) and not isinstance(mean, bool):
            series.append(float(mean))
    return series


def _group_for(sessions: list[dict[str, Any]], signal: str) -> str:
    for s in sessions:
        g = (s.get("signal_stats") or {}).get(signal, {}).get("group")
        if g:
            return g
    return ""


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
           threshold, threshold_kind, sessions_to_threshold, recommendation}``.
        Signals that are stable or drifting away from limits are omitted.
    """
    if alarm_thresholds is None:
        from analytics.thresholds import load_alarm_thresholds
        alarm_thresholds = load_alarm_thresholds()

    # All signals that appear anywhere.
    all_signals: set[str] = set()
    for s in sessions:
        all_signals.update((s.get("signal_stats") or {}).keys())

    forecasts: list[dict[str, Any]] = []
    for signal in sorted(all_signals):
        thr = alarm_thresholds.get(signal)
        if not thr:
            continue
        series = _signal_series(sessions, signal)
        if len(series) < MIN_SESSIONS:
            continue

        slope = theil_sen_slope(series)
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

        forecasts.append({
            "signal": signal,
            "group": _group_for(sessions, signal),
            "direction": "increasing" if slope > 0 else "decreasing",
            "slope_per_session": round(slope, 6),
            "current_mean": round(current, 6),
            "threshold": threshold,
            "threshold_kind": kind,
            "sessions_to_threshold": round(sessions_to, 1),
            "n_sessions": len(series),
            "recommendation": rec,
        })

    return sorted(forecasts, key=lambda f: f["sessions_to_threshold"])


__all__ = ["forecast_maintenance", "MIN_SESSIONS"]
