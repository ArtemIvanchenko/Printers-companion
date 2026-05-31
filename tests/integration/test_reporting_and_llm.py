import asyncio
from datetime import datetime, timezone

from domain.enums.common import DataQualityStatus
from domain.services.ingestion import IngestedFile
from domain.schemas.parsing import CanonicalEventDraft, FileClassification, ParseResult
from reporting.json_report.generator import generate_session_json_report
from reporting.llm.evidence_package import build_evidence_package
from reporting.llm.providers.null import NullLLMProvider
from reporting.markdown_report.generator import generate_markdown_report


def test_report_generation_preserves_version_metadata_and_non_llm_fallback() -> None:
    file = IngestedFile(
        path="job.log",
        relative_path="job.log",
        classification=FileClassification(path="job.log", file_name="job.log", family="main_event_log", role="primary", confidence=1.0),
        checksum="abc",
        size_bytes=10,
        data_quality_status=DataQualityStatus.ok,
        mtime=datetime(2026, 4, 27, tzinfo=timezone.utc),
        parse_result=ParseResult(
            parser_name="test",
            parser_version="0",
            file_family="main_event_log",
            role="primary",
            events=[CanonicalEventDraft(event_type="start", ts=datetime(2026, 4, 27, tzinfo=timezone.utc))],
        ),
    )
    report = generate_session_json_report("s1", [file])
    markdown = generate_markdown_report(report)
    evidence = build_evidence_package(report)

    assert "analysis_version" in report["version_metadata"]
    assert "# Session Report: s1" in markdown
    assert evidence.timeline[0]["event_type"] == "start"


def test_null_llm_provider_is_graceful() -> None:
    result = asyncio.run(NullLLMProvider().generate_markdown({"structured": "evidence"}))
    assert result.success is False
    assert "disabled" in result.error
