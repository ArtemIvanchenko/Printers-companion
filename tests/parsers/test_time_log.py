from pathlib import Path

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
    assert result.metadata["duration_mismatch_counts"]["pour"] == 1
    assert result.metadata["duration_mismatch_counts"]["burn"] == 1
    assert "partial_recovery" in result.data_quality
    assert {item.code for item in result.diagnostics} >= {
        "time_log_duration_mismatch", "time_log_duplicate_layers",
    }
