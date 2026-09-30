"""Cross-projection contracts, complete inputs and no chart-induced decisions."""
from copy import deepcopy
from datetime import datetime

import pytest

from analytics.normalization.deduplication import deduplicate_events
from domain.schemas.parsing import CanonicalEventDraft, FileClassification, ParseResult
from domain.services.ingestion import IngestedFile, IngestionService
from domain.services.session_analysis import prepare_session_analysis
from domain.services.session_overview import build_group_overview
from profiles.m350.profile import build_registry, get_profile
from reporting.json_report.generator import generate_session_json_report


def event_file(events):
    return IngestedFile(
        path="18.07.2026.log", relative_path="18.07.2026.log", checksum="events",
        size_bytes=1, data_quality_status="ok", mtime=datetime(2026, 7, 18),
        classification=FileClassification(path="18.07.2026.log", file_name="18.07.2026.log",
                                          family="main_event_log", role="primary", confidence=1),
        parse_result=ParseResult(parser_name="test", parser_version="1", file_family="main_event_log",
                                 role="primary", events=events),
    )


def sensor_file(tmp_path, name, values):
    path = tmp_path / name
    path.write_text("Time|SO1|SP4|\n" + "".join(
        f"12:{i // 60:02}:{i % 60:02}|{value}|1.0|\n" for i, value in enumerate(values)
    ))
    return IngestionService(build_registry(), get_profile()).parse(path).files[0]


def test_snapshot_is_shared_without_recomputing_features(monkeypatch):
    event = CanonicalEventDraft(event_type="pause", ts=datetime(2026, 7, 18, 12))
    files = [event_file([event, event.model_copy(deep=True)])]
    original = deepcopy(files[0].parse_result.events)
    prepared = prepare_session_analysis(files)
    overview = build_group_overview("s", files, analysis=prepared)
    monkeypatch.setattr("reporting.json_report.generator.prepare_session_analysis",
                        lambda *a, **k: pytest.fail("second interpretation"))
    report = generate_session_json_report("s", files, analysis=prepared, overview=overview)
    assert report["session_summary"]["features"] == overview["features"]
    assert report["analysis_snapshot"] == overview["analysis_snapshot"]
    assert report["analysis_snapshot"] is not overview["analysis_snapshot"]
    assert report["session_summary"]["features"]["pause_count"] == 1
    assert files[0].parse_result.events == original
    assert report["version_metadata"]["generated_by"]
    assert report["version_metadata"]["input_fingerprint"]


def test_different_measured_attempts_are_not_semantic_duplicates():
    first = CanonicalEventDraft(event_type="layer_timing_summary", payload={"layer": 1, "burn_ms": 1000})
    second = first.model_copy(update={"payload": {"layer": 1, "burn_ms": 2000}})
    assert len(deduplicate_events([first, second])[0]) == 2


def test_mismatching_snapshot_is_rejected():
    files = [event_file([])]
    overview = build_group_overview("s", files)
    with pytest.raises(ValueError, match="different analyses"):
        generate_session_json_report("s", files, overview=overview)


def test_graph_budget_does_not_change_health_and_full_signal_stats(tmp_path, monkeypatch):
    values = [0.1] * 1000
    values[509] = 10.0
    files = [sensor_file(tmp_path, "18.07.2026_sensors.log", values)]
    first = build_group_overview("s", files)
    monkeypatch.setattr("domain.services.session_telemetry._MAX_TELEMETRY_POINTS", 7)
    second = build_group_overview("s", files)
    assert len(first["telemetry"]["time"]) == 150
    assert len(second["telemetry"]["time"]) == 7
    assert first["health"] == second["health"]
    assert first["signal_stats"] == second["signal_stats"]
    assert first["data_quality"] == second["data_quality"]
    assert first["features"] == second["features"]
    assert first["signal_stats"]["SO1"]["n"] == 1000
    assert first["signal_stats"]["SO1"]["max"] == 10.0
    assert first["health"]["anomalies"]
    assert first["telemetry_evidence"]["sample_count"] == 1000


def test_statistics_include_all_daily_sources(tmp_path):
    files = [sensor_file(tmp_path, "18.07.2026_sensors.log", [0.1] * 20),
             sensor_file(tmp_path, "19.07.2026_sensors.log", [0.9] * 20)]
    overview = build_group_overview("s", files)
    assert overview["signal_stats"]["SO1"]["n"] == 40
    assert overview["signal_stats"]["SO1"]["mean"] == 0.5
    assert overview["telemetry_evidence"]["source_file_count"] == 2


def test_identical_bytes_on_distinct_clock_dates_are_distinct_measurements(tmp_path):
    files = [sensor_file(tmp_path, "18.07.2026_sensors.log", [0.1] * 20),
             sensor_file(tmp_path, "19.07.2026_sensors.log", [0.1] * 20)]
    assert files[0].checksum == files[1].checksum
    overview = build_group_overview("s", files)
    assert overview["signal_stats"]["SO1"]["n"] == 40


def test_empty_print_window_cannot_fall_back_to_outside_samples(tmp_path):
    from domain.services.session_telemetry import analysis_telemetry
    source = sensor_file(tmp_path, "18.07.2026_sensors.log", [21.0] * 20)
    telemetry, stats, evidence = analysis_telemetry(
        [source], datetime(2026, 7, 18, 15), datetime(2026, 7, 18, 16),
    )
    assert telemetry["time"] == []
    assert stats == {}
    assert evidence["sample_count"] == 0


def test_ml_reads_measured_snapshot_not_mutated_display_projection():
    from analytics.prediction.defect_risk import build_feature_row
    overview = build_group_overview("s", [event_file([])])
    expected = build_feature_row(overview)
    overview["features"]["layers"] = 999999
    assert build_feature_row(overview) == expected


def test_current_ml_artifact_cannot_score_legacy_display_features():
    from analytics.prediction.defect_risk import predict_defect_risk
    from core.versioning.constants import ANALYSIS_VERSION
    model = {"type": "logreg", "features": ["layers"], "mean": [0.0], "scale": [1.0],
             "coef": [0.01], "intercept": 0.0, "analysis_version": ANALYSIS_VERSION}
    legacy = predict_defect_risk({"features": {"layers": 100}}, model)
    assert legacy["method"] == "heuristic"
    assert legacy["model_not_applied_reason"] == "incompatible_analysis_version"
    assert legacy["prediction"]["warnings"]
    current = build_group_overview("s", [event_file([])])
    assert predict_defect_risk(current, model)["method"] == "model"


def test_missing_source_is_not_reported_as_complete(tmp_path):
    source = sensor_file(tmp_path, "18.07.2026_sensors.log", [0.1] * 20)
    # Alter only the detached DTO: no original file is deleted.
    source.path = str(tmp_path / "missing_sensors.log")
    overview = build_group_overview("s", [source])
    assert overview["telemetry_evidence"]["complete_available_stream"] is False
    assert overview["telemetry_evidence"]["source"] == "bounded_parser_tables"
    assert overview["telemetry_evidence"]["missing_file_count"] == 1


def test_burn_log_is_not_discarded_before_profile_parser(tmp_path):
    path = tmp_path / "18.07.2026_burn.log"
    path.write_text("Time|N|SO1|\n12:00:00|1|0.1|\n12:01:00|2|0.1|\n")
    result = IngestionService(build_registry(), get_profile()).parse(path)
    assert not result.skipped
    assert result.files[0].parse_result.parser_name == "burn_log"


def test_required_analysis_does_not_execute_optional_models(monkeypatch):
    monkeypatch.setattr("analytics.process_monitoring.build_advanced_monitoring",
                        lambda *a, **k: pytest.fail("shadow model blocked import"))
    overview = build_group_overview("s", [event_file([])])
    assert overview["advanced_monitoring"]["status"] == "not_requested"
    assert overview["advanced_monitoring"]["operator_action_allowed"] is False
