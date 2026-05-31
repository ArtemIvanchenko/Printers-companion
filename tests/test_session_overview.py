from datetime import datetime, timezone

from domain.enums.common import DataQualityStatus
from domain.schemas.parsing import (
    CanonicalEventDraft,
    FileClassification,
    ParsedTableBatch,
    ParseResult,
)
from domain.services.ingestion import IngestedFile
from domain.services.session_overview import build_group_overview


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
