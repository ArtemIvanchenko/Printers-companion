import json

import pytest

from analytics.cross_session import detect_signal_trends
from analytics.phase_statistics import _summary
from analytics.prediction.deviation_forecast import forecast_signal_deviations
from analytics.prediction.maintenance import _sessions_to_for_slope, forecast_maintenance
from analytics.soft_sensors import compute_soft_sensors
from analytics.telemetry_parser import compute_signal_stats


def sessions(values):
    return [{"session_id": str(index), "start_ts": f"2026-01-{index + 1:02d}",
             "signal_stats": {"SP4": {"mean": value, "group": "pressure"}}}
            for index, value in enumerate(values)]


def test_cross_session_slope_uses_all_session_positions():
    result = detect_signal_trends(sessions([1, None, 3, None, 5]), slope_threshold_pct=1)
    assert result[0]["slope_per_session"] == 1
    assert result[0]["n_sessions"] == 3


def test_phase_slope_is_per_physical_layer_instead_of_per_observation():
    result = _summary({1: 10, 3: 30, 8: 80}, "scan")
    assert result["theil_sen_sec_per_layer"] == 10


def test_signal_stats_trend_keeps_filtered_sensor_row_positions():
    values = [float(index) if index % 2 == 0 else None for index in range(20)]
    result = compute_signal_stats({"SP4": values}, valid_ranges={})
    assert result["SP4"]["trend_slope"] == 1
    assert result["SP4"]["n"] == 10


def test_signal_stats_trend_keeps_sentinel_and_range_row_positions():
    values = [float(index) if index % 2 == 0 else 999999999 for index in range(20)]
    result = compute_signal_stats({"SP4": values}, valid_ranges={})
    assert result["SP4"]["trend_slope"] == 1


def test_unknown_solver_result_does_not_become_zero_trend_in_stats(monkeypatch):
    monkeypatch.setattr("analytics.telemetry_parser.theil_sen_slope", lambda *a, **k: None)
    result = compute_signal_stats({"SP4": list(range(10))}, valid_ranges={})
    assert result["SP4"]["trend_slope"] is None
    json.dumps(result, allow_nan=False)


def test_missing_maintenance_observations_do_not_shorten_time_axis():
    result = forecast_maintenance(sessions([1, None, 3, 4, None, 6]), {"SP4": {"alarm_high": 10}})
    assert result[0]["slope_per_session"] == 1
    assert result[0]["sessions_to_threshold"] == 4
    assert result[0]["n_sessions"] == 4


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf")])
def test_missing_latest_measurement_cannot_be_current_maintenance_state(missing):
    assert forecast_maintenance(sessions([1, 2, 3, 4, missing]), {"SP4": {"alarm_high": 10}}) == []


def test_unknown_maintenance_solver_has_no_resource_forecast(monkeypatch):
    monkeypatch.setattr("analytics.prediction.maintenance.theil_sen_slope_ci", lambda *a, **k: (None, None, None))
    assert forecast_maintenance(sessions([1, 2, 3, 4]), {"SP4": {"alarm_high": 10}}) == []


@pytest.mark.parametrize("slope", [None, float("nan"), float("inf")])
def test_unknown_candidate_slope_is_not_a_resource_interval(slope):
    assert _sessions_to_for_slope(5, 10, slope, increasing=True) is None


def test_deviation_forecast_retains_missing_session_positions():
    result = forecast_signal_deviations(sessions([1, None, 3, 4, None, 6, 7, 8]), {})
    assert result[0]["trend_per_session"] == 1
    assert result[0]["predicted_next_mean"] == 9
    assert result[0]["prediction"]["sample_size"] == 6


def test_deviation_forecast_does_not_use_stale_current_measurement():
    assert forecast_signal_deviations(sessions([1, 2, 3, 4, 5, 6, None]), {}) == []


def test_unknown_deviation_solver_cannot_claim_normal_next_session(monkeypatch):
    monkeypatch.setattr("analytics.prediction.deviation_forecast.theil_sen_slope", lambda *a, **k: None)
    assert forecast_signal_deviations(sessions([1, 2, 3, 4, 5, 6, 7]), {}) == []


def test_unknown_soft_sensor_trend_is_omitted_without_flat_claim(monkeypatch):
    monkeypatch.setattr("analytics.soft_sensors.theil_sen_slope", lambda *a, **k: None)
    result = compute_soft_sensors({"pressure": {"SP4": [1, 2, 3, 4, 5]}})
    assert result["metrics"] == []


def test_nonfinite_soft_sensor_inputs_never_escape_into_json():
    result = compute_soft_sensors({}, {"SP11": {"mean": float("inf")}, "SP12": {"mean": 1}})
    assert result["metrics"] == []
    json.dumps(result, allow_nan=False)


def test_history_cap_preserves_thirty_observations_and_their_gaps():
    values = [float(index) if index % 2 == 0 else None for index in range(80)] + [80.0]
    result = forecast_maintenance(sessions(values), {"SP4": {"alarm_high": 100}})[0]
    assert result["n_sessions"] == 30
    assert result["slope_per_session"] == 1
    assert result["sessions_to_threshold"] == 20


def test_unknown_phase_solver_is_reported_as_null(monkeypatch):
    monkeypatch.setattr("analytics.phase_statistics.theil_sen_slope", lambda *a, **k: None)
    assert _summary({1: 10, 3: 30, 8: 80}, "scan")["theil_sen_sec_per_layer"] is None


def test_nonfinite_session_means_cannot_produce_nonfinite_findings():
    result = detect_signal_trends(sessions([1, float("inf"), 3, float("nan"), 5]), slope_threshold_pct=1)
    assert result[0]["slope_per_session"] == 1
    assert result[0]["n_sessions"] == 3
    json.dumps(result, allow_nan=False)


def test_pressure_drift_retains_irregular_row_positions():
    result = compute_soft_sensors({"pressure": {"SP4": [1, 2, None, None, 5, 6, None, None, None, 10]}})
    # Every finite pair has slope1 per original row; observed span9, median5.
    assert result["metrics"][0]["value"] == 180


def test_zero_pressure_reference_cannot_be_reported_as_flat_percentage():
    assert compute_soft_sensors({"pressure": {"SP4": [0] * 5}})["metrics"] == []
