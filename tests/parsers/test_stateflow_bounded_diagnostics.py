"""Bounded examples do not become the structural count or erase raw facts."""

from datetime import datetime, timedelta
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from analytics.data_quality import assess_session_quality
from api.main import app
from core.config.settings import get_settings
from domain.models.sessions import SourceFile
from domain.schemas.parsing import ParsedTableBatch, ParseDiagnosticRecord, ParseResult
from domain.services.ingestion import IngestionService
from domain.services.importing.persistence import persist_parse_results_to_db
from domain.services.session_analysis import prepare_session_analysis
from domain.services.session_overview import build_group_overview
from parsers.formats.stateflow_log import StateFlowLogParser
from profiles.m350.profile import build_registry, get_profile
from reporting.json_report.generator import generate_session_json_report, summarize_data_quality
from reporting.markdown_report.generator import generate_markdown_report
from storage.db.session import SessionLocal
from storage.repositories.reports import ReportsRepository
from storage.repositories.runtime import RuntimeRepository


def result_with_counts(metadata, tables=()):
    return ParseResult(parser_name="counter-fixture", parser_version="0", role="primary",
                       file_family="stateflow_log", metadata=metadata, tables=list(tables))


@pytest.mark.parametrize("count", [0, 1, 21, 50000])
def test_non_tabular_exact_metadata_counter(count):
    result = result_with_counts({"malformed_rows": count})
    assert result.malformed_row_count == count
    assert "malformed_row_count" not in result.model_dump()
    assert "malformed_row_count" not in result.model_json_schema()["properties"]


@pytest.mark.parametrize("invalid", [True, False, 21.0, "21", None, -1, float("nan"), float("inf")])
def test_invalid_metadata_counter_is_not_coerced_to_a_row_fact(invalid):
    assert result_with_counts({"malformed_rows": invalid}).malformed_row_count == 0


def test_tables_are_authoritative_without_metadata_double_count():
    result = result_with_counts({"malformed_rows": 999}, [
        ParsedTableBatch(malformed_rows=2, unknown_columns=["opaque"]),
        ParsedTableBatch(malformed_rows=3),
    ])
    assert result.malformed_row_count == 5
    result.tables = [ParsedTableBatch(malformed_rows=0)]
    assert result.malformed_row_count == 0


@pytest.mark.parametrize("invalid", [True, 2.0, None, -1])
def test_unknown_unvalidated_table_counter_does_not_become_a_row_fact(invalid):
    table = ParsedTableBatch.model_construct(malformed_rows=invalid)
    result = result_with_counts({"malformed_rows": 999})
    result.tables = [table]
    assert result.malformed_row_count == 0


def parsed_source(tmp_path, count=100):
    start = datetime(2026, 6, 8, 10)
    header = "Time;State;Mystery\n"
    lines = [f"{(start + timedelta(seconds=index)):%H:%M:%S};{1 if index < count-1 else 2}\n"
             for index in range(count)]
    path = tmp_path / "08.06.2026_stateFlow.log"
    path.write_text(header + "".join(lines), encoding="utf-8")
    file = IngestionService(build_registry(), get_profile()).parse(path).files[0]
    return file, lines, len(header.encode())


@pytest.mark.parametrize("count", [1, 20, 21, 100])
def test_malformed_examples_have_an_exact_total_and_original_byte_offsets(tmp_path, count):
    file, lines, offset = parsed_source(tmp_path, count)
    result = file.parse_result
    examples = [item for item in result.diagnostics if item.code == "malformed_stateflow_row"]
    assert len(examples) == min(count, 20)
    for index, example in enumerate(examples):
        assert example.source_line == index + 2
        assert example.source_offset == offset
        assert example.context == {"raw": lines[index].rstrip("\n")}
        offset += len(lines[index].encode())
    aggregate = [item for item in result.diagnostics if item.code == "malformed_stateflow_rows_aggregated"]
    assert len(aggregate) == (1 if count > 20 else 0)
    if aggregate:
        assert aggregate[0].context == {"total": count, "examples_retained": 20}
    assert result.metadata["row_count"] == result.metadata["parsed_timestamps"] == count
    assert result.metadata["malformed_rows"] == result.malformed_row_count == count
    assert "partial_recovery" in result.data_quality


def test_bounded_and_unbounded_diagnostics_have_the_same_quality_count_and_penalty(tmp_path):
    file, _, _ = parsed_source(tmp_path)
    compact = assess_session_quality([file], [])
    result = file.parse_result
    bounded = result.diagnostics
    result.diagnostics = [ParseDiagnosticRecord(severity="warning", code="malformed_stateflow_row",
                                               message="Column-count mismatch")
                          for _ in range(100)]
    unbounded = assess_session_quality([file], [])
    result.diagnostics = bounded
    assert compact == unbounded
    [issue] = compact["issues"]
    assert issue["count"] == issue["malformed_rows"] == 100
    assert issue["codes"] == ["malformed_stateflow_row"]
    # Existing penalty, independently: 100 - 8*min(2,1+log10(100)) = 84.
    assert compact["score"] == 84
    summary = summarize_data_quality([file], [])
    assert summary["malformed_rows"] == 100
    assert len(summary["parse_diagnostics"]) == 21


def test_late_unknown_state_and_all_transition_anchors_survive_diagnostic_cap(tmp_path):
    file, lines, _ = parsed_source(tmp_path)
    path = tmp_path / "08.06.2026_stateFlow.log"
    lines[50] = "10:00:50;1;opaque-value\n"
    path.write_text("Time;State;Mystery\n" + "".join(lines), encoding="utf-8")
    file = IngestionService(build_registry(), get_profile()).parse(path).files[0]
    result = file.parse_result
    assert result.malformed_row_count == 99
    assert len(result.transitions) == 3
    assert result.transitions[0].new_state == {"Mystery": "opaque-value"}
    assert result.transitions[1].previous_state == {"Mystery": "opaque-value"}
    assert result.transitions[1].new_state == {"Mystery": None}
    assert [item.duration_sec for item in result.transitions] == [50, 1, 48]
    assert [item.changed_columns for item in result.transitions] == [["Mystery"], ["Mystery"], ["State"]]
    assert sha256(path.read_bytes()).hexdigest() == file.checksum


def test_report_api_and_source_reference_preserve_exact_count_and_input_provenance(tmp_path):
    file, _, _ = parsed_source(tmp_path)
    analysis = prepare_session_analysis([file])
    overview = build_group_overview("bounded-stateflow-session", [file], analysis=analysis)
    report = generate_session_json_report("bounded-stateflow-session", [file], analysis=analysis, overview=overview)
    assert report["data_quality"]["malformed_rows"] == 100
    assert overview["data_quality"]["issues"][0]["count"] == 100
    assert "Malformed rows recovered: 100" in generate_markdown_report(report)
    assert report["version_metadata"]["input_file_hashes"] == {file.relative_path: file.checksum}
    assert report["version_metadata"]["parser_versions"]["stateflow_log"] == "0.2.3"
    assert StateFlowLogParser.version == "0.2.3"
    source_uri = f"s3://raw-logs/source/{file.checksum}"
    with SessionLocal() as db:
        RuntimeRepository(db).save_session_payload("bounded-stateflow-session", {"group": overview},
                                                  origin_compute_node_id=get_settings().compute_node_id)
        ReportsRepository(db).save_prepared(report["report_id"], {
            "storage_uri": None, "payload": report, "version_metadata": report["version_metadata"],
        })
        db.commit()
    persist_parse_results_to_db("bounded-stateflow-session", [file],
                                source_objects={file.relative_path: source_uri})
    with SessionLocal() as db:
        source = db.scalar(select(SourceFile))
        assert source.object_uri == source_uri
        assert source.checksum == file.checksum
        assert source.size_bytes == file.size_bytes
        assert source.original_path == file.path
    response = TestClient(app).get("/sessions/bounded-stateflow-session/parse-diagnostics")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 21
    assert response.json()[-1]["context"] == {"total": 100, "examples_retained": 20}
    assert sha256((tmp_path / "08.06.2026_stateFlow.log").read_bytes()).hexdigest() == file.checksum
