from datetime import datetime, timedelta, timezone

from analytics.prediction.deviation_forecast import forecast_signal_deviations
from analytics.prediction.maintenance import forecast_maintenance


def _sessions(values, signal="SP4", with_dates=False):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "session_id": f"s{index}",
            "start_ts": (start + timedelta(days=index * 2)).isoformat() if with_dates else None,
            "signal_stats": {signal: {"mean": value, "group": "pressure"}},
        }
        for index, value in enumerate(values)
    ]


def test_next_session_forecast_uses_empirical_backtest_interval():
    result = forecast_signal_deviations(
        _sessions([1.0, 2.1, 2.9, 4.2, 4.8, 6.1, 7.0, 8.1]),
        {"SP4": {"alarm_high": 20}},
    )[0]

    assert result["predicted_next_mean"] > result["current_mean"]
    assert result["prediction"]["interval"] is not None
    low, high = result["prediction"]["interval"]
    assert low <= result["predicted_next_mean"] <= high
    assert "backtest" in result["prediction"]["explanation"]


def test_next_session_forecast_flags_threshold_interval_risk():
    result = forecast_signal_deviations(
        _sessions([4, 5, 6, 7, 8, 9, 9.5, 9.8]),
        {"SP4": {"alarm_high": 10}},
    )[0]

    assert result["status"] == "threshold_risk"


def test_remaining_useful_life_has_calendar_projection_and_reliability():
    forecast = forecast_maintenance(
        _sessions(list(range(1, 13)), with_dates=True),
        {"SP4": {"alarm_high": 20}},
    )[0]
    rul = forecast["remaining_useful_life"]

    assert rul["sessions"] == forecast["sessions_to_threshold"]
    assert rul["average_days_between_prints"] == 2.0
    assert rul["calendar_days"] == rul["sessions"] * 2
    assert rul["reliability"] in {"medium", "high"}
