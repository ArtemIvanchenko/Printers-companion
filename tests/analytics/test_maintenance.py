"""Tests for analytics.prediction.maintenance.forecast_maintenance."""
from analytics.prediction.maintenance import _sessions_to_for_slope, forecast_maintenance


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


class TestSessionsToForSlope:
    """A candidate slope opposite in sign to the point estimate must be treated
    as "does not reach the threshold" (None), never as "already past" (0.0).

    Regression for a bug where ``_sessions_to_for_slope`` inferred "already
    past" from the CANDIDATE slope's own sign instead of the point estimate's
    fixed drift direction — a CI bound crossing zero (very common on noisy
    signals, exactly where an honest interval matters most) then fabricated a
    spurious 0-session bound even when the point estimate was nowhere close.
    """

    def test_opposite_sign_candidate_is_open_ended_not_urgent(self):
        # Increasing toward alarm_high, current well below threshold — a
        # negative candidate slope (CI says "could be flat/reversing") must
        # not collapse to "already past".
        assert _sessions_to_for_slope(90, 100, -0.5, increasing=True) is None

    def test_opposite_sign_candidate_for_falling_signal_is_open_ended(self):
        # Decreasing toward alarm_low, current well above threshold — a
        # positive candidate slope must not collapse to "already past" either.
        assert _sessions_to_for_slope(50, 40, 0.5, increasing=False) is None

    def test_already_past_is_independent_of_candidate_slope_sign(self):
        # Already past the threshold: true for ANY candidate slope, positive
        # or negative — it is a fact about current vs threshold, not slope.
        assert _sessions_to_for_slope(105, 100, 4.0, increasing=True) == 0.0
        assert _sessions_to_for_slope(105, 100, -1.0, increasing=True) == 0.0

    def test_same_sign_candidate_computes_normally(self):
        assert _sessions_to_for_slope(90, 100, 4.0, increasing=True) == 2.5


def test_noisy_rising_interval_brackets_the_point_estimate():
    # Fixed noisy upward series whose Theil-Sen confidence interval crosses
    # zero (low_slope < 0 < high_slope) despite a clearly positive point
    # slope — the exact shape that triggered the fabricated-bound bug.
    means = [47.18, 46.41, 56.41, 49.16, 58.57, 57.85]
    sessions = _sessions(means, signal="SP")
    out = forecast_maintenance(sessions, {"SP": {"alarm_high": 200.0}})
    assert len(out) == 1
    f = out[0]
    low, high = f["sessions_to_threshold_interval"]
    assert low <= f["sessions_to_threshold"] <= high
    # The point estimate is tens of sessions out — the interval must not
    # claim "0 sessions" (urgent now) just because the CI crosses zero.
    assert low > 0
    assert any("не определена статистически" in w for w in f["prediction"]["warnings"])


def test_noisy_falling_interval_brackets_the_point_estimate():
    means = [99.24, 98.96, 102.79, 93.45, 92.13, 91.4]
    sessions = _sessions(means, signal="SP")
    out = forecast_maintenance(sessions, {"SP": {"alarm_low": -50.0}})
    assert len(out) == 1
    f = out[0]
    low, high = f["sessions_to_threshold_interval"]
    assert low <= f["sessions_to_threshold"] <= high
    assert low > 0


def test_already_past_threshold_interval_is_degenerate_at_zero():
    sessions = _sessions([6, 7, 8, 9, 11])  # last already > 10
    out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
    assert out[0]["sessions_to_threshold_interval"] == [0.0, 0.0]
