from pathlib import Path

import pytest

from domain.services.ingestion import IngestionService
from domain.services.session_analysis import prepare_session_analysis
from domain.services.session_overview import build_group_overview
from profiles.m350.profile import build_registry, get_profile


def _parse(tmp_path: Path, folder: str, filename: str, contents: str):
    parent = tmp_path / folder
    parent.mkdir()
    path = parent / filename
    path.write_text(contents, encoding="utf-8")
    result = IngestionService(build_registry(), get_profile()).parse(path)
    assert len(result.files) == 1
    return result.files[0]


def _time(tmp_path):
    return _parse(tmp_path, "time", "14.10.2026_time.log",
                  "OLD_STATS: 1|1000|2000|4000|\n"
                  "NEW_STATS: L1_detailed|MakeLayer_Start:0|Pour_Start:0|Pour_End:1000|Burn_Start:2000|Burn_End:4000|Layer_End:4000|\n")


def _overview(files):
    analysis = prepare_session_analysis(files)
    group = build_group_overview("pause-source-test", files, analysis=analysis)
    assert group["analysis_snapshot"]["time_accounting"] == group["log_insights"]["time_accounting"]
    assert group["features"]["explicit_pause_seconds"] == group["log_insights"]["time_accounting"]["explicit_pause_seconds"]
    assert group["log_insights"]["time_accounting"]["observed_attempt_cycle_seconds"] == 4
    return group["log_insights"]["time_accounting"]


@pytest.mark.parametrize("source", [
    "missing", "empty", "undated", "partial", "out_of_order", "unparsed", "good_and_partial",
])
def test_absent_or_known_partial_pause_source_does_not_publish_zero(tmp_path, source):
    files = [_time(tmp_path)]
    if source != "missing":
        text = {
            "empty": "",
            "undated": "unknown clock -45878 1 Прожиг\n",
            "partial": "12:00:10 -45878 1 Прожиг\nunknown clock waiting\n",
            "out_of_order": "12:00:10 -45878 1 Прожиг\n12:00:01 waiting\n",
            "unparsed": "12:00:10 -45878 1 Прожиг\n",
            "good_and_partial": "12:00:10 -45878 1 Прожиг\nunknown clock waiting\n",
        }[source]
        main = _parse(tmp_path, "main", "14.10.2026.log", text)
        if source == "unparsed":
            main.parse_result = None
        files.append(main)
        if source == "good_and_partial":
            files.append(_parse(tmp_path, "complete-main", "14.10.2026.log", "12:00:10 -45878 1 Прожиг\n"))
    result = _overview(files)
    for key in ("explicit_pause_seconds", "confirmed_explicit_pause_seconds", "open_pause_count"):
        assert result[key] is None


@pytest.mark.parametrize("source,expected", [
    ("clean_no_pause", (0, 0, 0)),
    ("complete_pause", (10, 10, 0)),
    ("unresolved_pause", (None, 0, 1)),
    ("partial_with_measured_interval", (10, 10, 0)),
])
def test_observed_pause_evidence_is_preserved(tmp_path, source, expected):
    text = "12:00:10 -45878 1 Прожиг\n"
    if source == "partial_with_measured_interval":
        text += "unknown clock waiting\n"
    if source != "clean_no_pause":
        text += "12:00:20 ПАУЗА\n"
    if source in {"complete_pause", "partial_with_measured_interval"}:
        text += "12:00:30 Продолжение\n"
    main = _parse(tmp_path, "main", "14.10.2026.log", text)
    result = _overview([_time(tmp_path), main])
    assert tuple(result[key] for key in (
        "explicit_pause_seconds", "confirmed_explicit_pause_seconds", "open_pause_count")) == expected
