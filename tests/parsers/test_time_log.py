from pathlib import Path

import pytest

from parsers.base.base import ParserContext
from parsers.formats.time_log import TimeLogParser


def test_time_log_cross_checks_summary_against_absolute_markers(tmp_path: Path) -> None:
    path = tmp_path / "job_time.log"
    path.write_text(
        "OLD_STATS: 1|100|200|350|\n"
        "NEW_STATS: L1_detailed|Pour_Start:1000|Pour_End:1100|"
        "Burn_Start:1100|Burn_End:1300|MakeLayer_Start:950|Layer_End:1300|\n",
        encoding="utf-8",
    )

    result = TimeLogParser().parse(path, ParserContext())

    assert result.metadata["paired_layers"] == 1
    assert result.metadata["duration_consistency_score"] == 100.0
    assert result.metadata["duration_mismatch_counts"] == {
        "pour": 0, "burn": 0, "make_layer": 0,
    }


def test_time_log_surfaces_inconsistent_and_duplicate_layers(tmp_path: Path) -> None:
    path = tmp_path / "job_time.log"
    path.write_text(
        "OLD_STATS: 1|100|200|300|\n"
        "NEW_STATS: L1_detailed|Pour_Start:1000|Pour_End:1500|"
        "Burn_Start:1500|Burn_End:1400|MakeLayer_Start:1000|Layer_End:1400|\n"
        "OLD_STATS: 1|100|200|300|\n",
        encoding="utf-8",
    )

    result = TimeLogParser().parse(path, ParserContext())

    assert result.metadata["duplicate_layer_summaries"] == 1
    assert result.metadata["equivalent_layer_duplicates"] == 1
    assert result.metadata["conflicting_layer_duplicates"] == 0
    assert result.metadata["duration_mismatch_counts"]["pour"] == 1
    assert result.metadata["duration_mismatch_counts"]["burn"] == 1
    assert "partial_recovery" in result.data_quality
    assert {item.code for item in result.diagnostics} >= {
        "time_log_duration_mismatch", "time_log_duplicate_layers",
    }


def test_time_log_separates_repeat_attempts_and_rejects_impossible_cycle(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job_time.log"
    path.write_text(
        "OLD_STATS: 40|8000|10000|18400|\n"
        "OLD_STATS: 40|8000|20000|28400|\n"
        "OLD_STATS: 41|8000|10000|1000|\n",
        encoding="utf-8",
    )

    result = TimeLogParser().parse(path, ParserContext())

    assert result.metadata["conflicting_layer_duplicates"] == 1
    assert result.metadata["equivalent_layer_duplicates"] == 0
    assert result.metadata["impossible_cycle_count"] == 1
    assert "partial_recovery" in result.data_quality
    diagnostics = {item.code: item for item in result.diagnostics}
    assert diagnostics["time_log_duplicate_layers"].context["conflicting_layers"] == [40]
    assert diagnostics["time_log_impossible_cycle"].context["layers"] == [41]


def test_consistency_score_counts_only_available_absolute_comparisons(
    tmp_path: Path,
) -> None:
    path = tmp_path / "job_time.log"
    path.write_text(
        "OLD_STATS: 1|100|200|350|\n"
        "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:1300|\n",
        encoding="utf-8",
    )

    result = TimeLogParser().parse(path, ParserContext())

    assert result.metadata["duration_mismatch_counts"]["burn"] == 1
    assert result.metadata["duration_consistency_score"] == 0.0
    assert "partial_recovery" in result.data_quality
    summary = result.events[0]
    assert summary.payload["timing_valid"] is False
    assert summary.payload["timing_validation"] == "mismatch"


def test_missing_detail_does_not_shift_validation_to_previous_attempt(tmp_path):
    path = tmp_path / "retry_time.log"
    path.write_text(
        "OLD_STATS: 1|100|200|350|\n"
        "OLD_STATS: 1|100|600|750|\n"
        "NEW_STATS: L1_detailed|Pour_Start:1000|Pour_End:1100|"
        "Burn_Start:1100|Burn_End:1700|MakeLayer_Start:950|Layer_End:1700|\n"
    )
    result = TimeLogParser().parse(path, ParserContext())
    assert result.metadata["duration_consistency_score"] == 100.0
    assert result.metadata["unpaired_summaries"] == 1
    summaries = [e.payload for e in result.events if e.event_type == "layer_timing_summary"]
    assert [s["timing_validation"] for s in summaries] == ["summary_only", "verified"]


@pytest.mark.parametrize("line", [
    "OLD_STATS: 1|100|200|350oops|",
    "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:1300oops|",
    "NEW_STATS: L1_detailed|Burn_Start:1000|Burn_End:1300|Burn_End:1400|",
    "NEW_STATS: L1_detailed|Burn_Start:-1000|Burn_End:1300|",
])
def test_corrupted_numeric_fields_are_not_silently_truncated(tmp_path, line):
    path = tmp_path / "corrupt_time.log"
    path.write_text(line + "\n")
    result = TimeLogParser().parse(path, ParserContext())
    assert result.events == []
    assert "partial_recovery" in result.data_quality
    assert result.diagnostics[0].code == "time_log_malformed_rows"
