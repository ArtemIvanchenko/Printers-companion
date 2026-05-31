from pathlib import Path

from domain.enums.common import SourceFileFamily
from domain.services.file_classifier import classify_file
from parsers.base.base import ParserContext
from parsers.base.registry import ParserRegistry


def test_file_classifier_prioritizes_specific_patterns() -> None:
    assert classify_file(Path("job_burn.log")).family == SourceFileFamily.burn_log
    assert classify_file(Path("job_Monitor100.log")).family == SourceFileFamily.monitor100_log
    assert classify_file(Path("job_stateFlow.log")).family == SourceFileFamily.stateflow_log
    assert classify_file(Path("plain.log")).family == SourceFileFamily.main_event_log


def test_parser_registry_returns_structured_unsupported_result(tmp_path) -> None:
    path = tmp_path / "unknown.bin"
    path.write_bytes(b"abc")
    result = ParserRegistry().parse(path, SourceFileFamily.unsupported, ParserContext())
    assert result.data_quality == ["unsupported"]
    assert result.diagnostics[0].code == "unsupported_file_family"

