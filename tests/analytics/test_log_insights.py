from datetime import datetime
from collections import Counter

import pytest

from analytics.log_insights.clocks import burn_windows, pause_intervals, seconds
from analytics.log_insights.environment import analyze_environment, sensor_samples
from analytics.log_insights.geometry import compare_repeats, geometry_residuals, inspection_map, scan_reference
from analytics.log_insights.timing import time_accounting, restart_layer_comparison
from analytics.prediction.timing_evidence import summarize_timing_events


def timing(layer=1, burn=2000, pour=1000, make=4000):
    return {"event_type": "layer_timing_summary", "payload": {
        "layer": layer, "burn_ms": burn, "pour_ms": pour, "make_layer_ms": make}}


def stamp(kind, t, layer=None):
    return {"event_type": kind, "ts": f"2026-09-09T00:00:{t:02d}", "layer": layer}


def window(start=0, end=10, layer=1):
    return {"start": start, "end": end, "layer": layer, "precision": "logged_start"}


THRESHOLDS = {"SO1": {"high": 1, "unit": "%", "confirmed": True}}


def test_exposure_integrates_threshold_crossing_only_inside_burn():
    report = analyze_environment([(0, {"SO1": 0}), (10, {"SO1": 2})],
                                 [window()], [], THRESHOLDS, max_gap_s=10)
    metric = report["metrics"][0]
    assert metric["exceedance_seconds"] == pytest.approx(5)
    assert metric["excess_integral"] == pytest.approx(2.5)
    assert metric["coverage_ratio"] == 1
    assert report["affected_layer_count"] == 1


def test_segment_is_clipped_at_burn_boundaries():
    report = analyze_environment([(0, {"SO1": 0}), (10, {"SO1": 2})],
                                 [window(6, 8)], [], THRESHOLDS, max_gap_s=10)
    assert report["metrics"][0]["observed_seconds"] == 2
    assert report["metrics"][0]["excess_integral"] == pytest.approx(0.8)


def test_missing_values_and_long_gaps_are_not_filled():
    report = analyze_environment([(0, {"SO1": 2}), (1, {"SO1": None}),
                                  (2, {"SO1": 2}), (20, {"SO1": 2})],
                                 [window(0, 20)], [], THRESHOLDS)
    assert report["metrics"] == []
    assert report["rejected_gaps"] == 1


def test_duplicate_and_reversed_samples_do_not_double_count():
    report = analyze_environment([(0, {"SO1": 2}), (1, {"SO1": 2}), (1, {"SO1": 2}),
                                  (0.5, {"SO1": 2}), (2, {"SO1": 2})],
                                 [window(0, 2)], [], THRESHOLDS)
    assert report["metrics"][0]["exceedance_seconds"] == 2
    assert report["out_of_order_rows"] == 2


def test_recovery_requires_continuous_observations():
    pauses = [{"start": 0, "end": 10, "resumed": True}]
    samples = [(t, {"SO1": 0 if t >= 12 else 2}) for t in range(10, 20)]
    report = analyze_environment(samples, [], pauses, THRESHOLDS, stable_s=3)
    assert report["recovery"]["items"][0]["recovery_seconds"] == 2
    missing = analyze_environment([(10, {"SO1": 0}), (30, {"SO1": 0})], [], pauses, THRESHOLDS, stable_s=3)
    assert missing["recovery"]["items"][0]["recovery_seconds"] is None


def test_explicit_recovery_channels_cannot_be_silently_omitted():
    report = analyze_environment([(t, {"SO1": 0}) for t in range(10, 20)], [],
                                 [{"start": 0, "end": 10, "resumed": True}],
                                 {**THRESHOLDS, "SO2": {"high": 1}}, stable_s=3,
                                 required_signals=["SO1", "SO2"])
    assert report["recovery"]["items"][0]["recovery_seconds"] is None


def test_pause_and_burn_windows_exclude_ambiguous_overlaps():
    events = [timing(), timing(2), stamp("burn_start", 1, 1), stamp("burn_start", 4, 2),
              stamp("pause", 2), stamp("pause", 2), stamp("resume", 4)]
    pauses = pause_intervals(events)
    assert len(pauses) == 1
    assert [w["layer"] for w in burn_windows(events, pauses)] == [2]
    events.append(stamp("burn_start", 5, 2))
    assert burn_windows(events, pauses) == []


def test_timezone_conversion_is_consistent_for_naive_machine_clock():
    assert seconds(datetime(2026, 9, 9, 3)) == seconds("2026-09-09T00:00:00+00:00")


def test_cycle_components_conserve_measured_time_and_keep_pauses_separate():
    events = [timing(), timing(2, 10000, 1000, 22000)]
    report = time_accounting(events, [{"start": 0, "end": 100, "resumed": True}],
                             {"layer_overhead_ms": 500, "minimum_layer_cycle_ms": 4000})
    assert sum(c["seconds"] for c in report["components"]) == 26
    parts = {c["key"]: c["seconds"] for c in report["components"]}
    assert parts["base_overhead"] == 1
    assert parts["minimum_cycle_wait"] == 0.5
    assert report["normal_unique_layer_seconds"] == 15.5
    assert report["explicit_pause_seconds"] == 100
    assert report["observed_attempt_cycle_seconds"] == 26


def test_retry_copy_is_not_a_third_physical_attempt():
    events = [timing(), timing(burn=1000, make=3000), timing(burn=1000, make=3000)]
    evidence = summarize_timing_events(events)
    assert len(evidence.attempts) == 2
    assert evidence.equivalent_duplicate_rows == 1
    report = time_accounting(events)
    assert report["repeat_attempt_seconds"] == 3
    assert report["normal_unique_layer_seconds"] is None


def snapshot():
    return {"build_origin_source": "explicit", "build_origin_z_mm": 0,
            "layer_thickness_mm": 1, "laser_count": 1,
            "scan_timing_reference": {"beta": [1, 0, 0, 0, 0, 0], "source": "heuristic"},
            "scan_geometry": {"zs": [0, 20], "hatch_mm": [10, 10], "contour_mm": [0, 0],
                              "jump_mm": [0, 0], "n_jumps": [0, 0], "open_mm": [0, 0], "z_min": 0, "z_max": 20},
            "geometry_regions": [{"name": "деталь", "z_min_mm": 2, "z_max_mm": 10,
                                  "active_z_intervals_mm": [[2, 4], [8, 10]]}]}


def test_geometry_residuals_distinguish_systematic_bias_and_single_outlier():
    timings = {i: {"burn_ms": 11000} for i in range(1, 21)}
    timings[7] = {"burn_ms": 30000}
    report = geometry_residuals(timings, snapshot())
    assert report["median_bias_pct"] == pytest.approx(10)
    assert report["atypical_layer_count"] == 1
    assert report["items"][0]["layer"] == 7


def test_unknown_origin_never_invents_model_height():
    snap = snapshot()
    snap["build_origin_source"] = "inferred"
    assert geometry_residuals({1: {"burn_ms": 1000}}, snap)["status"] == "insufficient_data"
    env = {"layer_items": [{"layer": 7, "signals": {"SO1": {"exceedance_seconds": 2}}}]}
    mapped = inspection_map(env, {}, snap)
    assert mapped["items"][0]["z_range_mm"] is None
    assert mapped["items"][0]["active_bodies"] == []


def test_inspection_map_respects_gaps_in_body_sections():
    env = {"layer_items": [{"layer": 7, "signals": {"SO1": {"exceedance_seconds": 2}}}]}
    result = inspection_map(env, {}, snapshot())
    assert result["items"][0]["z_range_mm"] == [6, 7]
    assert result["items"][0]["active_bodies"] == []


def test_repeats_require_identity_and_compare_physical_layers_without_warping():
    target = {"session_id": "a", "comparison_key": "same", "timings": {1: {"burn_ms": 1000}, 3: {"burn_ms": 3000}}}
    ref = {"session_id": "b", "comparison_key": "same", "timings": {1: {"burn_ms": 2000}, 2: {"burn_ms": 3000}}}
    result = compare_repeats(target, [ref, {**ref, "comparison_key": "different"}])
    assert len(result["items"]) == 1
    assert result["items"][0]["common_layers"] == 1
    assert result["items"][0]["metrics"]["burn_ms"]["median_change_pct"] == -50
    assert compare_repeats({**target, "comparison_key": None}, [ref])["status"] == "insufficient_identity"


def test_sensor_stream_handles_midnight_headers_and_sentinels(tmp_path):
    from types import SimpleNamespace
    path = tmp_path / "09.09.2026_sensors.log"
    path.write_text("Time|SO1|Flow T|\n23:59:59|1|125|\nTime|SO1|Flow T|\n00:00:01|2|20|\n")
    source = SimpleNamespace(path=str(path), checksum="one", classification=SimpleNamespace(family="sensors_log"))
    diagnostics = Counter()
    rows = list(sensor_samples([source, source], {**THRESHOLDS, "Flow T": {"high": 30}}, diagnostics))
    assert len(rows) == 2
    assert rows[1][0] - rows[0][0] == 2
    assert rows[0][1]["Flow T"] is None
    assert diagnostics["midnight_rollovers"] == 1


def test_restart_layer_comparison_has_real_before_and_after():
    events = [timing(i, burn=(1000 if i <= 2 else 2000)) for i in range(1, 5)]
    windows = [window(i*10, i*10+1, i) for i in range(1, 5)]
    result = restart_layer_comparison(events, windows, [{"start": 22, "end": 29, "resumed": True}])
    assert result["items"][0]["metrics"]["burn_ms"]["change_pct"] == 100


def test_frozen_scan_reference_matches_physics_coefficients():
    ref = scan_reference({"hatch_speed_mm_s": 1000, "jump_speed_mm_s": 2000, "jump_delay_ms": 1}, "steel", 0.1, 2)
    assert ref["beta"] == [0.002, 0.002, 0.001, 0.002, 0.002, 0]


def test_chart_preserves_multiple_midnights_and_clips_by_full_date(tmp_path):
    from types import SimpleNamespace
    from domain.services.session_overview import _full_range_sensor_telemetry

    path = tmp_path / "18.07.2026_sensors.log"
    path.write_text("Time|SO1|\n10:00:00|0.1|\n23:59:59|0.1|\n00:00:01|0.1|\n"
                    "23:59:59|0.1|\n00:00:01|0.1|\n23:59:59|0.1|\n00:15:29|0.1|\n")
    source = SimpleNamespace(path=str(path), checksum="multiday",
                             classification=SimpleNamespace(family="sensors_log"))
    report = _full_range_sensor_telemetry([source])
    assert report["timestamps"][0] == "2026-07-18T10:00:00"
    assert report["timestamps"][-1] == "2026-07-21T00:15:29"
    assert report["timestamp_diagnostics"]["midnight_rollovers"] == 3
    clipped = _full_range_sensor_telemetry([source], datetime(2026, 7, 20), datetime(2026, 7, 20, 12))
    assert clipped["timestamps"] == ["2026-07-20T00:00:01"]
