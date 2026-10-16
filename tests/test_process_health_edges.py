import json
from datetime import datetime, timedelta

import pytest

from analytics.process_health import (
    _clean_signal,
    _clean_signal_indexed,
    analyze_layer_burn_drift,
    atmosphere_readiness_score,
    detect_process_anomalies,
)


@pytest.mark.parametrize("raw", [[True] * 6, [False] * 6, [None] * 6, None])
def test_missing_or_boolean_analog_channel_has_unknown_readiness(raw):
    result = atmosphere_readiness_score({"oxygen": {"SO1": raw}})
    assert result["score"] is None
    assert result["grade"] == "unknown"
    assert result["factors"] == {}


def test_measured_zero_oxygen_is_not_a_missing_value():
    assert atmosphere_readiness_score({"oxygen": {"SO1": [0.0] * 6}})["score"] == 100


def test_zero_mean_variable_channel_does_not_receive_perfect_stability():
    result = atmosphere_readiness_score({"pressure": {"NO_PROFILE": [-1.0, 1.0] * 5}})
    assert result["score"] is None
    assert result["grade"] == "unknown"


def test_low_spike_has_negative_signed_deviation():
    result = detect_process_anomalies({"pressure": {"NO_PROFILE": [10.0] * 6 + [0.0]}})
    spike = next(item for item in result if item["kind"] == "spike")
    assert spike["z_score"] < 0
    assert spike["value"] == 0
    assert spike["sample_index"] == 6


def test_value_and_indexed_cleaning_share_ranges_sentinels_and_bool_policy(monkeypatch):
    monkeypatch.setattr("analytics.thresholds.load_valid_ranges", lambda: {
        "SENSOR": {"min_val": 0, "max_val": 20, "invalid_values": [125], "range_policy": "enforced"},
    })
    raw = [None, True, -1, 5, 125, 6, float("inf"), 1e30]
    assert _clean_signal("SENSOR", raw) == [5, 6]
    assert _clean_signal_indexed("SENSOR", raw) == [(3, 5), (5, 6)]


def test_physical_filter_keeps_anomaly_at_original_array_row():
    raw = [None, True, 10, 10, 10, 10, 10, 0]
    result = detect_process_anomalies({
        "time": [f"row-{index}" for index in range(len(raw))],
        "pressure": {"NO_PROFILE": raw},
    })
    spike = next(item for item in result if item["kind"] == "spike")
    assert spike["sample_index"] == 7
    assert spike["time"] == "row-7"
    assert spike["sample_count"] == 8


def test_mismatching_axis_length_cannot_supply_unverified_anomaly_time():
    result = detect_process_anomalies({
        "time": [f"row-{index}" for index in range(8)],
        "timestamps": [f"2026-07-18T12:00:{index:02d}" for index in range(8)],
        "oxygen": {"SO1": [0.1] * 5 + [3]},
    })
    assert result
    assert all("time" not in item and "timestamp" not in item for item in result)
    assert all(item["sample_index"] == 5 for item in result)


def test_layer_mapping_normalizes_mixed_datetimes_only_with_declared_zone():
    result = detect_process_anomalies({
        "clock_timezone": "Europe/Moscow",
        "timestamps": [f"2026-07-18T12:00:{index:02d}" for index in range(6)],
        "oxygen": {"SO1": [0.1] * 5 + [3]},
        "layer_time_points": [
            {"layer": 1, "timestamp": "2026-07-18T09:00:00+00:00"},
            {"layer": 2, "timestamp": "2026-07-18T12:00:05"},
        ],
    })
    assert all(item["layer"] == 2 for item in result)
    assert all(item["layer_time_distance_sec"] == 0 for item in result)


def test_unknown_zone_never_guesses_aware_to_naive_layer_mapping():
    result = detect_process_anomalies({
        "timestamps": [f"2026-07-18T12:00:{index:02d}" for index in range(6)],
        "oxygen": {"SO1": [0.1] * 5 + [3]},
        "layer_time_points": [{"layer": 1, "timestamp": "2026-07-18T09:00:00+00:00"}],
    })
    assert result
    assert all("layer" not in item and "layer_range" not in item for item in result)


@pytest.mark.parametrize("duration", [True, float("nan"), float("inf"), None])
def test_invalid_burn_duration_cannot_create_or_poison_a_trend(duration):
    result = analyze_layer_burn_drift([
        {"layer": 1, "duration_sec": 10}, {"layer": 2, "duration_sec": 20},
        {"layer": 3, "duration_sec": duration},
    ])
    assert result["trend"] == "insufficient_data"
    assert result["n_layers"] == 2
    assert result["slope_sec_per_layer"] is None
    json.dumps(result, allow_nan=False)


def test_missing_layer_identifier_cannot_become_physical_x():
    result = analyze_layer_burn_drift([
        {"layer": 1, "duration_sec": 10}, {"duration_sec": 20},
        {"layer": True, "duration_sec": 30},
    ])
    assert result["trend"] == "insufficient_data"
    assert result["n_layers"] == 1


def test_same_layer_x_does_not_imply_a_flat_process():
    result = analyze_layer_burn_drift([{"layer": 1, "duration_sec": value} for value in [10, 20, 30]])
    assert result["trend"] == "insufficient_data"
    assert result["slope_sec_per_layer"] is None
    assert result["n_layers"] == 1
    assert result.get("relative_change_pct") is None
    json.dumps(result, allow_nan=False)


def test_burn_slope_matches_manual_physical_layer_least_squares():
    result = analyze_layer_burn_drift([
        {"layer": 1, "duration_sec": 10}, {"layer": 3, "duration_sec": 30},
        {"layer": 8, "duration_sec": 80},
    ])
    assert result["slope_sec_per_layer"] == 10
    assert result["relative_change_pct"] == 175


def test_readiness_keeps_existing_weighted_formula_on_known_numbers():
    result = atmosphere_readiness_score({
        "oxygen": {"SO1": [21] * 6}, "pressure": {"SP4": [1] * 6},
    })
    # Oxygen: .35 stability+.65*0 safe=.35; pressure:1. Weights .5 and.3.
    assert result["score"] == round((.35 * .5 + 1 * .3) / .8 * 100, 1)


def test_repeated_observations_do_not_invent_a_third_physical_layer():
    result = analyze_layer_burn_drift([
        {"layer": 1, "duration_sec": 10}, {"layer": 1, "duration_sec": 10},
        {"layer": 2, "duration_sec": 20},
    ])
    assert result["n_layers"] == 2
    assert result["trend"] == "insufficient_data"


def test_unrepresentable_burn_arithmetic_stays_unknown_and_json_safe():
    result = analyze_layer_burn_drift([
        {"layer": layer, "duration_sec": 1e308} for layer in [1, 2, 3]
    ])
    assert result["trend"] == "insufficient_data"
    assert result["slope_sec_per_layer"] is None
    json.dumps(result, allow_nan=False)


def test_layer_cadence_is_prepared_once_for_shared_time_frame(monkeypatch):
    from analytics import process_health

    cadence_calls = []
    original_median = process_health.median
    def measured_median(values):
        if values == [60.0] * 4:
            cadence_calls.append(values)
        return original_median(values)
    monkeypatch.setattr(process_health, "median", measured_median)
    result = detect_process_anomalies({
        "timestamps": [f"2026-07-18T10:00:{second:02d}" for second in range(10, 16)],
        "oxygen": {"SO1": [21.0] * 6},
        "layer_time_points": [
            {"layer": layer + 1, "timestamp": f"2026-07-18T10:{layer:02d}:00"}
            for layer in range(5)
        ],
    })
    assert len(result) == 1
    assert result[0]["layer"] == 1
    assert result[0]["sample_ranges"][0]["end"]["layer_time_distance_sec"] == 15
    assert len(cadence_calls) == 1


def test_bounded_alarm_ranges_do_not_locate_discarded_ranges(monkeypatch):
    from analytics import process_health

    parsed = []
    class MeasuredDatetime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            parsed.append(value)
            return datetime.fromisoformat(value)
    monkeypatch.setattr(process_health, "datetime", MeasuredDatetime)
    start = datetime(2026, 7, 18, 10)
    result = detect_process_anomalies({
        "timestamps": [(start + timedelta(seconds=second)).isoformat() for second in range(120)],
        "oxygen": {"SO1": [.1, 21.0] * 60},
        "layer_time_points": [{"layer": 1, "timestamp": start.isoformat()}],
    })
    [alarm] = result
    assert alarm["alarm_fraction"] == .5
    assert alarm["sample_count"] == 120
    assert alarm["sample_index"] == 1
    assert [(item["start"]["sample_index"], item["end"]["sample_index"])
            for item in alarm["sample_ranges"]] == [(index, index) for index in range(1, 40, 2)]
    # One burn timestamp + two endpoints for each retained range + peak row.
    assert len(parsed) == 42


def test_nearest_layer_tie_retains_earlier_timestamp_and_original_row():
    result = detect_process_anomalies({
        "timestamps": ["2026-07-18T10:01:30"] * 6,
        "oxygen": {"SO1": [.1] * 5 + [21]},
        "layer_time_points": [
            {"layer": 10, "timestamp": "2026-07-18T10:00:00"},
            {"layer": 20, "timestamp": "2026-07-18T10:01:00"},
            {"layer": 30, "timestamp": "2026-07-18T10:02:00"},
        ],
    })
    assert all(item["sample_index"] == 5 for item in result)
    assert all(item["layer"] == 20 for item in result)
    assert all(item["layer_mapping_precision"] == "nearest_burn_log_timestamp" for item in result)
    assert all(item["layer_time_distance_sec"] == 30 for item in result)


def test_large_timestamp_gap_keeps_bracket_not_guessed_layer():
    result = detect_process_anomalies({
        "timestamps": ["2026-07-18T12:00:00"] * 6,
        "oxygen": {"SO1": [.1] * 5 + [21]},
        "layer_time_points": [
            {"layer": 1, "timestamp": "2026-07-18T10:00:00"},
            {"layer": 2, "timestamp": "2026-07-18T10:01:00"},
            {"layer": 3, "timestamp": "2026-07-18T10:02:00"},
            {"layer": 40, "timestamp": "2026-07-18T15:00:00"},
        ],
    })
    # Independent differences: cadence median(60,60,17880)=60 s; tolerance
    # max(300,2.5*60)=300 s. Nearest distance is 7080 s, hence the bracket.
    assert all("layer" not in item for item in result)
    assert all(item["layer_range"] == [3, 40] for item in result)
    assert all(item["layer_mapping_precision"] == "timestamp_gap_range" for item in result)
    assert all(item["layer_time_distance_sec"] == 7080 for item in result)
