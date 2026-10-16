"""Layer localization and burn duration keep their different evidence scopes."""

from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from analytics.process_health import analyze_layer_burn_drift, detect_process_anomalies
from domain.schemas.parsing import SourceLocation
from domain.services.ingestion import IngestionService
from domain.services.session_telemetry import _layer_burn_times, _layer_time_points, analysis_telemetry
from profiles.m350.profile import build_registry, get_profile


def parse_file(tmp_path, text, name="08.06.2026_time.log"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    result = IngestionService(build_registry(), get_profile()).parse(path)
    assert not result.skipped and not result.diagnostics
    return result.files[0]


def test_actual_night_markers_localize_the_same_sensor_frame(tmp_path):
    # Adjacent genuine 08 June rows; table and main log carry the same marker.
    main = parse_file(tmp_path, "23:59:42 -58019 207 Прожиг\n"
                      "00:00:24 -58079 208 Прожиг\n", "08.06.2026.log")
    burn = parse_file(tmp_path, "Time|N|\n23:59:42|207|\n00:00:24|208|\n",
                      "08.06.2026_burn.log")
    expected = [{"layer": 207, "timestamp": "2026-06-08T23:59:42"},
                {"layer": 208, "timestamp": "2026-06-09T00:00:24"}]
    assert _layer_time_points([burn, main]) == expected
    assert _layer_time_points([burn]) == expected
    target = expected[-1]["timestamp"]
    anomalies = detect_process_anomalies({
        "time": [target] * 12, "timestamps": [target] * 12,
        "oxygen": {"SO1": [.01] * 11 + [10]}, "layer_time_points": expected,
    })
    assert anomalies
    assert all(item["layer"] == 208 and item["layer_time_distance_sec"] == 0 for item in anomalies)
    assert all(item["layer_mapping_precision"] == "nearest_burn_log_timestamp" for item in anomalies)


def test_whole_file_main_facts_are_not_replaced_by_sampled_burn_clocks(tmp_path):
    start = datetime(2026, 6, 8, 23, 50)
    clocks = [(start + timedelta(seconds=index)).strftime("%H:%M:%S") for index in range(6000)]
    main = parse_file(tmp_path, "".join(f"{clock} -58000 {index} Прожиг\n"
                      for index, clock in enumerate(clocks, 1)), "08.06.2026.log")
    burn = parse_file(tmp_path, "Time|N|\n" + "".join(f"{clock}|{index}|\n"
                      for index, clock in enumerate(clocks, 1)), "08.06.2026_burn.log")
    assert burn.parse_result.metadata["sampled_rows"] == 5000
    assert burn.parse_result.metadata["total_rows"] == 6000
    points = _layer_time_points([burn, main])
    assert len(points) == 6000
    assert points[600] == {"layer": 601, "timestamp": "2026-06-09T00:00:00"}
    assert points[-1]["timestamp"] == "2026-06-09T01:29:59"


def test_main_anchors_are_deduplicated_but_partial_main_is_not_filled_with_table_guesses(tmp_path):
    main = parse_file(tmp_path, "00:00:24 -58079 208 Прожиг\n", "09.06.2026.log")
    burn = parse_file(tmp_path, "Time;N\n23:59:42;207\n00:00:24;208\n",
                      "08.06.2026_burn.log")
    assert _layer_time_points([burn, main, main.model_copy(deep=True)]) == [
        {"layer": 208, "timestamp": "2026-06-09T00:00:24"},
    ]


def test_missing_main_layer_or_timestamp_keeps_table_fallback_available(tmp_path):
    main = parse_file(tmp_path, "23:59:42 Старт прожига\n24:00:00 -58079 208 Прожиг\n",
                      "08.06.2026.log")
    burn = parse_file(tmp_path, "Time;N\n23:59:42;207\n00:00:24;208\n",
                      "08.06.2026_burn.log")
    assert _layer_time_points([main, burn])[-1] == {
        "layer": 208, "timestamp": "2026-06-09T00:00:24",
    }


def test_calendar_advances_even_when_a_row_has_no_usable_physical_layer(tmp_path):
    burn = parse_file(tmp_path, "Time;N\n23:59:59;unknown\n00:00:01;2\n",
                      "08.06.2026_burn.log")
    assert _layer_time_points([burn]) == [{"layer": 2, "timestamp": "2026-06-09T00:00:01"}]


def test_explicit_dates_do_not_roll_and_old_rows_do_not_erase_the_forward_anchor(tmp_path):
    burn = parse_file(tmp_path, "Time;N\n2026-06-09 23:59:59;1\n"
                      "2026-06-08 00:00:01;2\n00:00:03;3\n", "08.06.2026_burn.log")
    by_layer = {p["layer"]: p["timestamp"] for p in _layer_time_points([burn])}
    assert by_layer == {1: "2026-06-09T23:59:59", 2: "2026-06-08T00:00:01",
                        3: "2026-06-10T00:00:03"}


def test_table_observation_span_is_not_a_measured_laser_duration(tmp_path):
    # Genuine repeated-layer clocks from 19 May: 102 min 12 s between attempts.
    burn = parse_file(tmp_path, "Time;N\n15:21:41;40\n17:03:53;40\n",
                      "19.05.2026_burn.log")
    original = deepcopy(burn.parse_result.model_dump())
    assert _layer_burn_times([burn]) == []
    assert len(_layer_time_points([burn])) == 2
    assert analyze_layer_burn_drift(_layer_burn_times([burn]))["trend"] == "insufficient_data"
    assert burn.parse_result.model_dump() == original
    _, _, evidence = analysis_telemetry([burn])
    assert any("невидимые переходы суток" in note for note in evidence["limitations_ru"])


def test_new_stats_pairs_within_one_row_regardless_of_raw_key_order(tmp_path):
    file = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_End:31000|Burn_Start:1000|\n")
    assert _layer_burn_times([file]) == [{"layer": 1, "duration_sec": 30.0}]


def test_partial_old_and_new_measurements_share_one_admission(tmp_path):
    file = parse_file(tmp_path, "OLD_STATS: 1|9000|30000|39300|\n"
                      "NEW_STATS: L2_detailed|Burn_End:41000|Burn_Start:1000|\n")
    assert _layer_burn_times([file]) == [{"layer": 1, "duration_sec": 30.0},
                                       {"layer": 2, "duration_sec": 40.0}]


def test_new_stats_do_not_resurrect_a_known_invalid_old_summary(tmp_path):
    file = parse_file(tmp_path, "OLD_STATS: 1|9000|30000|39300|\n"
                      "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:41000|\n"
                      "NEW_STATS: L2_detailed|Burn_Start:1000|Burn_End:31000|\n")
    assert file.parse_result.events[0].payload["timing_valid"] is False
    assert _layer_burn_times([file]) == [{"layer": 2, "duration_sec": 30.0}]


def test_real_new_stats_repeated_attempts_are_not_joined_across_reset_clock_frames(tmp_path):
    file = parse_file(tmp_path, "NEW_STATS: L40_detailed|Burn_End:10823959|Burn_Start:10676800|"
                      "Layer_End:10823959|MakeLayer_Start:10668176|Pour_End:10676695|Pour_Start:10668400|\n"
                      "NEW_STATS: L40_detailed|Burn_End:246312|Burn_Start:140453|"
                      "Layer_End:246312|MakeLayer_Start:131867|Pour_End:140235|Pour_Start:131970|\n",
                      "19.05.2026_time.log")
    assert _layer_burn_times([file]) == []


def test_conflicting_cycle_or_pour_rejects_the_layer_even_with_equal_burn(tmp_path):
    file = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_Start:10000|Burn_End:40000|"
                      "Pour_Start:1000|Pour_End:10000|MakeLayer_Start:700|Layer_End:40000|\n"
                      "NEW_STATS: L1_detailed|Burn_Start:60000|Burn_End:90000|"
                      "Pour_Start:50000|Pour_End:60000|MakeLayer_Start:49000|Layer_End:90000|\n")
    assert _layer_burn_times([file]) == []


def test_equivalent_boundary_copies_are_one_duration_and_original_old_value_wins(tmp_path):
    detailed = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:31020|\n",
                          "08.06.2026_time.log")
    summary = parse_file(tmp_path, "OLD_STATS: 1|9000|30000|39300|\n", "09.06.2026_time.log")
    assert _layer_burn_times([detailed, detailed.model_copy(deep=True), summary]) == [
        {"layer": 1, "duration_sec": 30.0},
    ]


def test_partial_markers_are_not_joined_across_rows_or_files(tmp_path):
    combined = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_Start:1000|\n"
                           "NEW_STATS: L1_detailed|Burn_End:31000|\n")
    first = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_Start:1000|\n", "09.06.2026_time.log")
    second = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_End:31000|\n", "10.06.2026_time.log")
    assert _layer_burn_times([combined]) == []
    assert _layer_burn_times([first, second]) == []


def test_legacy_markers_without_row_provenance_cannot_be_paired(tmp_path):
    file = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:31000|\n")
    for event in file.parse_result.events:
        event.source = SourceLocation()
    assert _layer_burn_times([file]) == []


@pytest.mark.parametrize("duration,expected", [(0, []), (50, []), (100, [.1]),
                                              (3_600_000, [3600.0]), (3_600_001, [])])
def test_new_measurements_use_existing_shared_burn_admission_bounds(tmp_path, duration, expected):
    file = parse_file(tmp_path, f"NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:{1000+duration}|\n")
    assert [p["duration_sec"] for p in _layer_burn_times([file])] == expected


@pytest.mark.parametrize("invalid", [True, 1000.0, None, -1, float("nan")])
def test_unknown_marker_value_does_not_become_a_duration(tmp_path, invalid):
    file = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:31000|\n")
    file.parse_result.events[0].payload["abs_ms"] = invalid
    assert _layer_burn_times([file]) == []


def test_timing_projection_does_not_mutate_any_source_events_or_tables(tmp_path):
    file = parse_file(tmp_path, "OLD_STATS: 1|9000|30000|39300|\n"
                      "NEW_STATS: L1_detailed|Burn_Start:10000|Burn_End:40000|"
                      "Pour_Start:1000|Pour_End:10000|MakeLayer_Start:700|Layer_End:40000|\n")
    before = file.model_dump_json()
    assert _layer_burn_times([file]) == [{"layer": 1, "duration_sec": 30.0}]
    assert file.model_dump_json() == before


def test_pure_duration_projection_does_not_import_the_calibration_service(tmp_path, monkeypatch):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        assert name != "analytics.prediction.scan_calibration"
        return original(name, *args, **kwargs)
    file = parse_file(tmp_path, "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:31000|\n")
    monkeypatch.setattr(builtins, "__import__", guarded)
    assert _layer_burn_times([file]) == [{"layer": 1, "duration_sec": 30.0}]
