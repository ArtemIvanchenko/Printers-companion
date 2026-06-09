"""Tests for analytics.prediction.maintenance.forecast_maintenance."""
from analytics.prediction.maintenance import forecast_maintenance


def _sessions(means, signal="SP4", group="pressure"):
    return [
        {"session_id": f"s{i}", "signal_stats": {signal: {"mean": m, "group": group}}}
        for i, m in enumerate(means)
    ]


def test_rising_signal_forecasts_finite_horizon():
    # mean climbs 1.0 → 5.0; alarm_high at 10 → ~5 more sessions.
    sessions = _sessions([1, 2, 3, 4, 5])
    out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
    assert len(out) == 1
    f = out[0]
    assert f["direction"] == "increasing"
    assert f["threshold_kind"] == "alarm_high"
    assert 3 <= f["sessions_to_threshold"] <= 7
    assert "ТО" in f["recommendation"]


def test_stable_signal_no_forecast():
    sessions = _sessions([5.0, 5.0, 5.01, 4.99, 5.0])
    out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
    assert out == []


def test_signal_drifting_away_from_only_limit_skipped():
    # Falling signal but only an alarm_high exists → drifting away → skip.
    sessions = _sessions([9, 8, 7, 6, 5])
    out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
    assert out == []


def test_already_past_threshold_is_urgent():
    sessions = _sessions([6, 7, 8, 9, 11])  # last already > 10
    out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
    assert len(out) == 1
    assert out[0]["sessions_to_threshold"] == 0.0
    assert "сейчас" in out[0]["recommendation"]


def test_too_few_sessions_skipped():
    sessions = _sessions([1, 2, 3])  # below MIN_SESSIONS
    out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
    assert out == []


def test_no_threshold_for_signal_skipped():
    sessions = _sessions([1, 2, 3, 4, 5])
    out = forecast_maintenance(sessions, {})  # no thresholds at all
    assert out == []
