from pathlib import Path

from parsers.base.base import ParserContext
from parsers.formats.monitor100_log import Monitor100LogParser
from parsers.formats.stateflow_log import StateFlowLogParser
from parsers.formats.stateflowdata import StateFlowDataParser


def test_monitor100_recovers_glued_entries(tmp_path: Path) -> None:
    path = tmp_path / "job_Monitor100.log"
    path.write_text(
        "2026-04-27 10:00:00 A1=0 2026-04-27 10:00:01 A1=1\n",
        encoding="utf-8",
    )
    result = Monitor100LogParser().parse(path, ParserContext())

    assert len(result.events) == 2
    assert result.metadata["glued_entries_recovered"] == 1
    assert result.events[1].payload["value"] == "1"


def test_stateflow_streaming_rle_emits_only_transitions(tmp_path: Path) -> None:
    path = tmp_path / "job_stateFlow.log"
    path.write_text(
        "Timestamp;State;Failure;ChamberDoor\n"
        "2026-04-27 10:00:00;1;0;closed\n"
        "2026-04-27 10:00:01;1;0;closed\n"
        "2026-04-27 10:00:02;2;0;closed\n"
        "2026-04-27 10:00:03;2;1;closed\n",
        encoding="utf-8",
    )
    result = StateFlowLogParser().parse(path, ParserContext(profile_version="0.1.0"))

    assert result.metadata["streaming"] is True
    assert result.metadata["row_count"] == 4
    assert len(result.transitions) == 2
    assert result.transitions[0].changed_columns == ["State"]
    assert result.transitions[1].subsystem == "failure"


def test_stateflowdata_binary_is_preserved_as_unsupported_metadata(tmp_path: Path) -> None:
    path = tmp_path / "job_stateFlowData.log"
    path.write_bytes(b"\x00\x01\x02abc")
    result = StateFlowDataParser().parse(path, ParserContext())

    assert "unsupported" in result.data_quality
    assert result.metadata["format"] == "binary_or_unknown"

