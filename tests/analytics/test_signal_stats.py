"""Tests for analytics.signal_stats — per-session signal statistics extraction."""
import pytest
from analytics.signal_stats import compute_signal_stats


@pytest.fixture
def sample_telemetry():
    return {
        "oxygen": {
            "SO1": [0.1] * 100 + [2.5, 2.6],   # mostly normal, 2 spikes
            "SO2": [0.12 + i * 0.001 for i in range(102)],
        },
        "temperatures": {
            "ST5": [168.0 + i * 0.1 for i in range(100)],
        },
        "pressure": {
            "SP4": [1.001] * 80,
        },
        "humidity": {
            "Flow H": [18.5] * 50,
        },
    }


def test_returns_stats_for_all_signals(sample_telemetry):
    stats = compute_signal_stats(sample_telemetry)
    assert set(stats.keys()) == {"SO1", "SO2", "ST5", "SP4", "Flow H"}


def test_group_labels(sample_telemetry):
    stats = compute_signal_stats(sample_telemetry)
    assert stats["SO1"]["group"] == "oxygen"
    assert stats["ST5"]["group"] == "temperature"
    assert stats["SP4"]["group"] == "pressure"
    assert stats["Flow H"]["group"] == "humidity"


def test_mean_reasonable(sample_telemetry):
    stats = compute_signal_stats(sample_telemetry)
    # SO1: mostly 0.1 with two spikes at 2.5/2.6 → mean slightly above 0.1
    assert stats["SO1"]["mean"] > 0.1
    # SP4 is constant 1.001
    assert abs(stats["SP4"]["mean"] - 1.001) < 0.001


def test_p95_above_p05(sample_telemetry):
    """p95 should always be >= p05 regardless of distribution shape."""
    stats = compute_signal_stats(sample_telemetry)
    assert stats["SO1"]["p95"] >= stats["SO1"]["p05"]
    assert stats["ST5"]["p95"] >= stats["ST5"]["p05"]

def test_p95_above_mean_for_symmetric():
    """For a symmetric (uniform) distribution, p95 > mean."""
    uniform = [float(i) for i in range(100)]
    stats = compute_signal_stats({"oxygen": {"SO1": uniform}})
    assert stats["SO1"]["p95"] >= stats["SO1"]["mean"]


def test_empty_telemetry():
    assert compute_signal_stats({}) == {}


def test_short_series_skipped():
    # Series with < 5 values should be skipped.
    stats = compute_signal_stats({"oxygen": {"SO1": [0.1, 0.2, 0.3]}})
    assert stats == {}


def test_std_is_zero_for_constant():
    stats = compute_signal_stats({"pressure": {"SP4": [1.001] * 50}})
    assert stats["SP4"]["std"] == 0.0


def test_trend_slope_rising():
    # Linearly rising series → slope > 0.
    rising = [float(i) for i in range(100)]
    stats = compute_signal_stats({"oxygen": {"SO1": rising}})
    assert stats["SO1"]["trend_slope"] > 0


def test_trend_slope_falling():
    falling = [float(100 - i) for i in range(100)]
    stats = compute_signal_stats({"oxygen": {"SO1": falling}})
    assert stats["SO1"]["trend_slope"] < 0
