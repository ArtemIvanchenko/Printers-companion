from datetime import datetime, timezone

import pytest

from domain.enums.common import DataQualityStatus
from domain.schemas.parsing import (
    CanonicalEventDraft,
    FileClassification,
    ParsedTableBatch,
    ParseResult,
    StateTransitionDraft,
)
from domain.services.ingestion import IngestedFile
from domain.services.session_overview import build_group_overview, compute_session_spans


def _burn_file() -> IngestedFile:
    rows = [
        {"Time": "13:00:00", "N": 1, "SO1": 9.0, "SO2": 20.4, "ST3": 25.0, "ST5": 27.7, "SP4": 1.0004, "Flow H": 0.1},
        {"Time": "13:00:05", "N": 1, "SO1": 8.5, "SO2": 19.0, "ST3": 25.1, "ST5": 28.0, "SP4": 1.0002, "Flow H": 0.1},
        {"Time": "13:00:20", "N": 2, "SO1": 7.0, "SO2": 15.0, "ST3": 25.3, "ST5": 28.6, "SP4": 1.0001, "Flow H": 0.2},
    ]
    return IngestedFile(
        path="b_burn.log",
        relative_path="b_burn.log",
        classification=FileClassification(path="b_burn.log", file_name="b_burn.log", family="burn_log", role="primary", confidence=1.0),
        checksum="x",
        size_bytes=10,
        data_quality_status=DataQualityStatus.ok,
        mtime=datetime(2026, 3, 23, tzinfo=timezone.utc),
        parse_result=ParseResult(
            parser_name="burn_log",
            parser_version="0.1.0",
            file_family="burn_log",
            role="primary",
            tables=[ParsedTableBatch(rows=rows)],
            metadata={"total_rows": 3},
        ),
    )


def _event_file() -> IngestedFile:
    events = [
        CanonicalEventDraft(event_type="start", ts=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc), layer=1),
        CanonicalEventDraft(event_type="burn_event", ts=datetime(2026, 3, 23, 14, 0, tzinfo=timezone.utc), layer=2, phase="burn"),
        CanonicalEventDraft(event_type="pause", ts=datetime(2026, 3, 23, 14, 30, tzinfo=timezone.utc)),
    ]
    return IngestedFile(
        path="b.log",
        relative_path="b.log",
        classification=FileClassification(path="b.log", file_name="b.log", family="main_event_log", role="primary", confidence=1.0),
        checksum="y",
        size_bytes=10,
        data_quality_status=DataQualityStatus.ok,
        mtime=datetime(2026, 3, 23, tzinfo=timezone.utc),
        parse_result=ParseResult(
            parser_name="main_event_log",
            parser_version="0.1.0",
            file_family="main_event_log",
            role="primary",
            events=events,
            metadata={"line_count": 3},
        ),
    )


def test_spans_share_one_raw_pass_but_keep_distinct_print_and_burn_windows():
    source = _event_file()
    source.parse_result.events = [
        CanonicalEventDraft(event_type="finish", ts=datetime(2026, 3, 24, 1)),
        CanonicalEventDraft(event_type="note", ts=datetime(2026, 3, 23, 23), layer=0),
        CanonicalEventDraft(event_type="Burn_event", ts=datetime(2026, 3, 24, 0)),
        CanonicalEventDraft(event_type="note", ts=datetime(2026, 3, 23, 23, 30), phase=" BURN "),
        CanonicalEventDraft(event_type="burn_event", layer=2),
    ]
    source.parse_result.transitions = [StateTransitionDraft(
        ts_start=datetime(2026, 3, 23, 22), ts_end=datetime(2026, 3, 24, 12),
        parser_version="test",
    )]
    monitor = source.model_copy(deep=True)
    monitor.parse_result.file_family = "monitor100_log"
    monitor.parse_result.events[0].ts = datetime(2026, 3, 20)
    monitor.parse_result.transitions[0].ts_start = datetime(2026, 3, 26)
    original = source.model_copy(deep=True)
    spans = compute_session_spans([monitor, source])
    assert spans == (
        (datetime(2026, 3, 23, 22), datetime(2026, 3, 24, 1)),
        (datetime(2026, 3, 23, 23), datetime(2026, 3, 24, 0)),
    )
    assert source == original


@pytest.mark.parametrize("copies", [0, 1, 2])
def test_burn_span_needs_distinct_timestamped_events(copies):
    source = _event_file()
    timestamp = datetime(2026, 3, 23, tzinfo=timezone.utc)
    source.parse_result.events = [
        CanonicalEventDraft(event_type="burn_event", ts=timestamp) for _ in range(copies)
    ]
    print_span, burn_span = compute_session_spans([source])
    assert print_span == ((timestamp, timestamp) if copies else (None, None))
    assert burn_span == (None, None)


def test_overview_has_classification_and_dashboard_features():
    files = [_burn_file(), _event_file()]
    ov = build_group_overview(
        "auto_group_1", files,
        start_ts=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc),
        end_ts=datetime(2026, 3, 23, 14, 30, tzinfo=timezone.utc),
    )
    assert ov["classification"] == "REAL_PRINT"
    feats = ov["features"]
    for key in ("first_time", "last_time", "duration_min", "total_lines",
                "total_events", "layers", "burn_events", "file_count",
                "pause_count", "material"):
        assert key in feats
    assert feats["total_events"] == 3
    assert feats["burn_events"] == 1
    assert feats["pause_count"] == 1
    assert feats["layers"] == 2
    assert feats["file_count"] == 2
    assert feats["first_time"] == "13:00"
    assert ov["advanced_monitoring"]["mode"] == "shadow"
    assert ov["advanced_monitoring"]["operator_action_allowed"] is False


def test_overview_telemetry_decodes_signals():
    ov = build_group_overview("g", [_burn_file()])
    tel = ov["telemetry"]
    assert "oxygen" in tel and "SO1" in tel["oxygen"]
    assert "temperatures" in tel and "ST5" in tel["temperatures"]
    assert "pressure" in tel and "SP4" in tel["pressure"]
    assert tel["oxygen"]["SO1"] == [9.0, 8.5, 7.0]
    # per-layer burn durations derived from N + Time
    burns = {b["layer"]: b["duration_sec"] for b in tel["layer_burn_times"]}
    assert burns[1] == 5.0  # 13:00:00 -> 13:00:05


def test_overview_handles_files_without_tables():
    ov = build_group_overview("g", [_event_file()])
    assert ov["telemetry"] == {} or ov["telemetry"].get("layer_burn_times") == []
    assert ov["features"]["total_events"] == 3


def _time_log_file(layer_seconds: dict[int, tuple[float, float]]) -> IngestedFile:
    """layer -> (burn_ms, pour_ms), as a parsed time_log with layer_timing_summary events."""
    events = [
        CanonicalEventDraft(
            event_type="layer_timing_summary",
            payload={"layer": layer, "burn_ms": burn_ms, "pour_ms": pour_ms},
        )
        for layer, (burn_ms, pour_ms) in layer_seconds.items()
    ]
    return IngestedFile(
        path="t_time.log",
        relative_path="t_time.log",
        classification=FileClassification(path="t_time.log", file_name="t_time.log", family="time_log", role="secondary", confidence=1.0),
        checksum="z",
        size_bytes=10,
        data_quality_status=DataQualityStatus.ok,
        mtime=datetime(2026, 3, 23, tzinfo=timezone.utc),
        parse_result=ParseResult(
            parser_name="time_log",
            parser_version="0.1.0",
            file_family="time_log",
            role="secondary",
            events=events,
            metadata={},
        ),
    )


def test_overview_does_not_mislabel_unmeasured_residual_as_idle():
    # Wall span (event file): 13:00 -> 14:30 = 90 min = 5400s.
    # Machine time (time_log): 2 layers x (20s burn + 10s pour) = 60s.
    # The gap also contains ordinary cycle overhead and unmeasured layers.
    files = [_event_file(), _time_log_file({1: (20_000, 10_000), 2: (20_000, 10_000)})]
    ov = build_group_overview(
        "g_idle", files,
        start_ts=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc),
        end_ts=datetime(2026, 3, 23, 14, 30, tzinfo=timezone.utc),
    )
    feats = ov["features"]
    assert feats["machine_seconds"] == 60.0
    assert feats["unattributed_elapsed_seconds"] == 5340.0
    assert feats["idle_seconds"] is None
    assert feats["idle_pct"] is None
    assert feats["explicit_pause_seconds"] == 0.0
    assert feats["open_pause_count"] == 1


def test_overview_idle_none_without_time_log():
    ov = build_group_overview("g_noidle", [_event_file()])
    feats = ov["features"]
    assert feats["machine_seconds"] is None
    assert feats["idle_seconds"] is None
    assert feats["idle_pct"] is None


def test_all_layer_burn_times_reach_health_analysis():
    timings = {layer: (10_000 + layer, 9_000) for layer in range(1, 1501)}
    overview = build_group_overview("g_long", [_time_log_file(timings)])
    assert len(overview["telemetry"]["layer_burn_times"]) == 1500
    assert overview["health"]["burn_drift"]["n_layers"] == 1500
