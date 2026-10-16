"""Repeated-header facts come from counters, not inferred legacy metadata."""

from types import SimpleNamespace

import pytest

from domain.schemas.parsing import ParsedTableBatch, ParseResult
from parsers.base.base import ParserContext
from parsers.formats.stateflow_log import StateFlowLogParser
from reporting.json_report.generator import summarize_data_quality


def result_with_counts(metadata, tables=()):
    return ParseResult(parser_name="counter-fixture", parser_version="0", role="primary",
                       file_family="stateflow_log", metadata=metadata, tables=list(tables))


@pytest.mark.parametrize("count", [0, 1, 2, 50000])
def test_exact_non_tabular_header_counter_is_not_a_serialized_field(count):
    result = result_with_counts({"repeated_headers": count})
    assert result.repeated_header_count == count
    assert "repeated_header_count" not in result.model_dump()
    assert "repeated_header_count" not in result.model_json_schema()["properties"]


@pytest.mark.parametrize("invalid", [True, False, 2.0, "2", None, -1, float("nan"), float("inf")])
def test_invalid_metadata_header_counter_is_not_coerced(invalid):
    assert result_with_counts({"repeated_headers": invalid}).repeated_header_count == 0


def test_table_header_counters_are_authoritative_without_double_counting():
    result = result_with_counts({"repeated_headers": 999}, [
        ParsedTableBatch(repeated_headers=2), ParsedTableBatch(repeated_headers=3),
    ])
    assert result.repeated_header_count == 5
    result.tables = [ParsedTableBatch(repeated_headers=0)]
    assert result.repeated_header_count == 0


@pytest.mark.parametrize("invalid", [True, 2.0, None, -1])
def test_unknown_unvalidated_table_header_count_is_not_a_fact(invalid):
    result = result_with_counts({"repeated_headers": 999})
    result.tables = [ParsedTableBatch.model_construct(repeated_headers=invalid)]
    assert result.repeated_header_count == 0


def test_legacy_header_rows_alone_do_not_imply_a_repeated_header_count():
    result = result_with_counts({"header_rows": 4})
    assert result.repeated_header_count == 0
    assert result.metadata == {"header_rows": 4}


def parse_text(tmp_path, text):
    path = tmp_path / "08.06.2026_stateFlow.log"
    path.write_text(text, encoding="utf-8")
    return StateFlowLogParser().parse(path, ParserContext(profile_id="m350"))


@pytest.mark.parametrize("text,headers,rows", [
    ("Time;State\n", 1, 0),
    ("Time;State\n" * 3, 3, 0),
    ("10:00:00 1\n10:00:00 1\n", 0, 2),
    ("Time;State\n10:00:00;1\nTime;State\n10:00:01;2\nTime;State\n", 3, 2),
])
def test_current_stateflow_metadata_and_report_count_only_repeated_headers(tmp_path, text, headers, rows):
    result = parse_text(tmp_path, text)
    assert result.metadata["header_rows"] == headers
    assert result.metadata["row_count"] == rows
    assert result.metadata["repeated_headers"] == result.repeated_header_count == max(0, headers - 1)
    file = SimpleNamespace(parse_result=result, relative_path="stateFlow.log", size_bytes=len(text.encode()))
    assert summarize_data_quality([file], [])["repeated_headers"] == max(0, headers - 1)


def test_row_cap_does_not_count_unparsed_headers_or_drop_malformed_transitions(tmp_path, monkeypatch):
    monkeypatch.setattr(StateFlowLogParser, "MAX_ROWS", 3)
    result = parse_text(tmp_path, "Time;State;Unknown\n" * 3 +
                        "10:00:00;1\n10:00:01;1\nTime;State;Unknown\n10:00:02;2\n" +
                        "Time;State;Unknown\n10:00:03;3\n")
    assert result.metadata["header_rows"] == 4
    assert result.repeated_header_count == 3
    assert result.metadata["row_count"] == result.malformed_row_count == 3
    assert result.metadata["truncated"] is True
    [transition] = result.transitions
    assert transition.new_state == {"State": 2}
    assert transition.duration_sec == 2
    assert next(item for item in result.diagnostics if item.code == "stateflow_rows_capped").source_line == 8


def test_transition_cap_counts_only_headers_before_existing_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(StateFlowLogParser, "MAX_TRANSITIONS", 2)
    result = parse_text(tmp_path, "Time;State\n10:00:00;1\nTime;State\n10:00:01;2\n"
                        "Time;State\n10:00:02;3\nTime;State\n10:00:03;4\n")
    assert result.metadata["header_rows"] == 3
    assert result.repeated_header_count == 2
    assert result.metadata["row_count"] == 3
    assert result.metadata["truncated"] is True
    assert [item.duration_sec for item in result.transitions] == [1, 1]
    assert next(item for item in result.diagnostics if item.code == "stateflow_transitions_capped").source_line == 6
