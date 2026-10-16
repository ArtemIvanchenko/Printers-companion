from datetime import datetime
import pytest

from parsers.base.base import ParserContext
from parsers.formats.error_log import ErrorLogParser
from parsers.formats.stateflow_log import StateFlowLogParser


def parse_text(tmp_path, name, text, parser):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return parser.parse(path, ParserContext(source_file_id="calendar-source"))


@pytest.mark.parametrize("header,rows", [
    ("Date;Time;State", "28.04.2026;23:59:59;1\n29.04.2026;00:00:01;2\n"),
    ("Time;Date;State", "23:59:59;28.04.2026;1\n00:00:01;29.04.2026;2\n"),
])
def test_stateflow_separate_date_time_columns_override_filename(tmp_path, header, rows):
    result = parse_text(tmp_path, "27.04.2026_stateFlow.log", header + "\n" + rows, StateFlowLogParser())
    transition = result.transitions[0]
    assert transition.ts_start == datetime(2026, 4, 28, 23, 59, 59)
    assert transition.ts_end == datetime(2026, 4, 29, 0, 0, 1)
    assert transition.duration_sec == 2
    assert transition.source_file_id == "calendar-source"
    assert result.metadata["midnight_rollovers"] == 0


@pytest.mark.parametrize("bad_date", ["31.02.2026", "", "unavailable"])
def test_stateflow_explicit_date_column_never_falls_back_to_filename(tmp_path, bad_date):
    result = parse_text(tmp_path, "27.04.2026_stateFlow.log",
        f"Date;Time;State\n{bad_date};10:00:00;1\n28.04.2026;10:00:01;2\n", StateFlowLogParser())
    assert result.transitions[0].ts_start is None
    assert result.transitions[0].duration_sec is None
    assert result.metadata["missing_timestamps"] == 1
    assert "partial_recovery" in result.data_quality


def test_stateflow_clock_rollover_preserves_elapsed_transition(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_stateFlow.log",
        "Time;State\n23:59:59;1\n00:00:01;2\n", StateFlowLogParser())
    assert result.transitions[0].duration_sec == 2
    assert result.transitions[0].ts_end == datetime(2026, 4, 28, 0, 0, 1)
    assert result.metadata["midnight_rollovers"] == 1


def test_stateflow_explicit_backward_date_does_not_become_zero_duration(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_stateFlow.log",
        "Timestamp;State\n2026-04-28 23:59:59;1\n2026-04-27 00:00:01;2\n", StateFlowLogParser())
    assert result.transitions[0].ts_end == datetime(2026, 4, 27, 0, 0, 1)
    assert result.transitions[0].duration_sec is None
    assert result.metadata["out_of_order_timestamps"] == 1


def test_stateflow_keeps_time_named_state_columns(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_stateFlow.log",
        "Time;Timeout;Update\n10:00:00;0;0\n10:00:01;1;1\n", StateFlowLogParser())
    assert result.transitions[0].changed_columns == ["Timeout", "Update"]


def test_stateflow_unknown_first_row_is_not_misidentified_as_header(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_stateFlow.log", "update failed\n", StateFlowLogParser())
    assert result.metadata["row_count"] == 1
    assert result.metadata["missing_timestamps"] == 1
    assert "partial_recovery" in result.data_quality


@pytest.mark.parametrize("header_count", [1, 3])
def test_header_only_stateflow_is_empty_evidence(tmp_path, header_count):
    result = parse_text(tmp_path, "27.04.2026_stateFlow.log", "Time;State\n" * header_count, StateFlowLogParser())
    assert not result.transitions
    assert "empty" in result.data_quality
    assert result.metadata["row_count"] == 0
    assert result.metadata["header_rows"] == header_count


@pytest.mark.parametrize("text", ["Time|     Error\r\n", "Time|     Error\r\n" * 6])
def test_real_error_header_is_not_a_machine_error(tmp_path, text):
    result = parse_text(tmp_path, "23.03.2026_error.log", text, ErrorLogParser())
    assert not result.events
    assert "empty" in result.data_quality
    assert result.metadata["header_rows"] == len(text.splitlines())
    assert any(d.code == "empty_error_log_low_trust" for d in result.diagnostics)


def test_error_header_filter_keeps_unknown_nonempty_entries(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_error.log",
        "Time|Error\nupdate failed\ntime error\n10:00:00|laser failure\nTime|Error\n", ErrorLogParser())
    assert [e.payload["raw_text"] for e in result.events] == [
        "update failed", "time error", "10:00:00|laser failure",
    ]
    assert [e.source.source_line for e in result.events] == [2, 3, 4]
    assert all(e.source.source_file_id == "calendar-source" for e in result.events)
    assert result.metadata["missing_timestamps"] == 2
    assert "partial_recovery" in result.data_quality


def test_error_clock_rollover_and_explicit_dates_are_distinct(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_error.log",
        "23:59:59|failure\n00:00:01|failure\n2026-04-28 00:00:02|failure\n", ErrorLogParser())
    assert [e.ts for e in result.events] == [
        datetime(2026, 4, 27, 23, 59, 59), datetime(2026, 4, 28, 0, 0, 1),
        datetime(2026, 4, 28, 0, 0, 2),
    ]
    assert result.metadata["midnight_rollovers"] == 1


def test_error_invalid_explicit_date_keeps_record_and_raw_timestamp(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_error.log",
        "2026-02-31 10:00:00|laser failure\n", ErrorLogParser())
    assert result.events[0].ts is None
    assert result.events[0].raw_timestamp == "2026-02-31 10:00:00"
    assert result.events[0].payload["raw_text"] == "2026-02-31 10:00:00|laser failure"


def test_nonzero_whitespace_error_log_is_empty_evidence(tmp_path):
    result = parse_text(tmp_path, "27.04.2026_error.log", " \n\t\n", ErrorLogParser())
    assert not result.events
    assert "empty" in result.data_quality
