"""Time-log structural counts must survive the existing quality/report path."""

from types import SimpleNamespace

import pytest

from analytics.data_quality import assess_session_quality
from parsers.base.base import ParserContext
from parsers.formats.time_log import TimeLogParser
from reporting.json_report.generator import summarize_data_quality


@pytest.mark.parametrize("malformed_count", [0, 1, 20, 100])
def test_time_log_preserves_exact_malformed_count_in_quality_and_report(tmp_path, malformed_count):
    path = tmp_path / "08.06.2026_time.log"
    damaged_lines = [
        "OLD_STATS: 2|100|200|",  # interrupted summary write
        "NEW_STATS: L2_detailed|",  # interrupted detail write
        "NEW_STATS: L2_detailed|Burn_Start:1000|Burn_End:1300oops|",  # damaged number
    ]
    valid_pair = (
        "OLD_STATS: 1|100|200|350|\n"
        "NEW_STATS: L1_detailed|Pour_Start:1000|Pour_End:1100|"
        "Burn_Start:1100|Burn_End:1300|MakeLayer_Start:950|Layer_End:1300|\n"
    )
    path.write_text(valid_pair + "".join(
        damaged_lines[index % len(damaged_lines)] + "\n" for index in range(malformed_count)
    ))
    result = TimeLogParser().parse(path, ParserContext(source_file_id="original-time-source"))
    source = SimpleNamespace(parse_result=result, relative_path=path.name, size_bytes=path.stat().st_size)

    assert result.metadata["malformed_rows"] == result.malformed_row_count == malformed_count
    assert result.metadata["summary_count"] == result.metadata["detailed_count"] == 1
    assert result.metadata["paired_layers"] == 1
    assert result.metadata["duration_consistency_score"] == 100
    assert result.events[0].payload["timing_validation"] == "verified"
    assert result.events[0].source.source_file_id == "original-time-source"
    assert len(result.events) == 7

    summary = summarize_data_quality([source], [])
    assert summary["malformed_rows"] == malformed_count
    quality = assess_session_quality([source], result.events)
    if malformed_count:
        [diagnostic] = result.diagnostics
        assert diagnostic.code == "time_log_malformed_rows"
        assert diagnostic.context["count"] == malformed_count
        [issue] = quality["issues"]
        assert issue["count"] == issue["malformed_rows"] == malformed_count
        assert issue["codes"] == ["time_log_malformed_rows"]
        # Existing penalty: 8 * min(2, 1 + log10(count)); no new score rule.
        assert quality["score"] == (92 if malformed_count == 1 else 84)
        assert result.data_quality == ["partial_recovery"]
    else:
        assert result.diagnostics == []
        assert quality["issues"] == []
        assert quality["score"] == 100
        assert result.data_quality == ["ok"]
