from datetime import datetime, timezone
from pathlib import Path

from analytics.normalization.timestamps import normalize_timestamps
from domain.enums.common import DataQualityStatus
from domain.services.ingestion import IngestedFile
from domain.services.session_classification import classify_session
from domain.services.session_grouping import group_files_into_sessions, manual_merge, manual_split
from domain.schemas.parsing import FileClassification, ParseResult, ParsedTableBatch


def test_timestamp_rollover_uses_next_day(tmp_path: Path) -> None:
    path = tmp_path / "20260427_time.log"
    values = ["23:59:59", "00:00:02"]
    normalized = normalize_timestamps(values, path)
    assert normalized[1].day == 28


def test_session_classification_requires_print_evidence_not_monitor_only() -> None:
    monitor_file = _file("m.log", "monitor100_log", "primary")
    monitor_result = classify_session([monitor_file])
    assert monitor_result.classification.value == "IDLE_DIAGNOSTIC"

    burn_file = _file("b.log", "burn_log", "primary")
    burn_file.parse_result.tables.append(ParsedTableBatch(rows=[{"Layer": 1}]))
    burn_result = classify_session([burn_file])
    assert burn_result.classification.value == "REAL_PRINT"


def test_manual_split_and_merge_are_auditable_operations() -> None:
    # Both files share the run prefix "job", i.e. they are one print — which is
    # what makes splitting them a *manual* operation worth auditing.
    files = [_file("job.log", "main_event_log", "primary"), _file("job_burn.log", "burn_log", "primary")]
    group = group_files_into_sessions(files)[0]
    assert len(group.files) == 2
    left, right = manual_split(group, {files[1].path})
    merged = manual_merge([left, right])
    assert left.reasons == ["manual_split"]
    assert merged.confidence == 1.0
    assert len(merged.files) == 2


def test_distinct_run_prefixes_are_never_merged() -> None:
    # Two prints of identical file family, four days apart, must NOT be merged.
    early = _file("23.03_burn.log", "burn_log", "primary", mtime=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc))
    late = _file("27.03_burn.log", "burn_log", "primary", mtime=datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc))
    groups = group_files_into_sessions([early, late])
    assert len(groups) == 2
    assert all(g.reasons[0] == "same_run_prefix" for g in groups)


def test_consecutive_prints_do_not_chain_into_one_session() -> None:
    """Regression: gap-based grouping compared each file with the PREVIOUS one,
    so a run of prints spaced under the threshold merged without limit — ten
    daily prints became a single session and every duration/count derived from
    it was wrong."""
    files = []
    for day in range(1, 11):
        stamp = datetime(2026, 3, day, 8, 0, tzinfo=timezone.utc)
        files.append(_file(f"{day:02d}.03.2026.log", "main_event_log", "primary", mtime=stamp))
        files.append(_file(f"{day:02d}.03.2026_sensors.log", "sensors_log", "secondary", mtime=stamp))

    groups = group_files_into_sessions(files)
    assert len(groups) == 10
    assert all(len(g.files) == 2 for g in groups)
    assert len({g.group_id for g in groups}) == 10


def test_one_print_stays_whole_despite_far_apart_file_anchors() -> None:
    """Files of one print anchor at very different times (a parsed sensors log
    anchors at the real print start, a table-only log at its filename date +
    mtime). Grouping must key on the shared run prefix, not on that spread."""
    day = datetime(2026, 3, 23, 6, 0, tzinfo=timezone.utc)
    files = [
        _file("23.03.2026.log", "main_event_log", "primary", mtime=day),
        _file("23.03.2026_sensors.log", "sensors_log", "secondary",
              mtime=day.replace(hour=20)),
        _file("23.03.2026_time.log", "time_log", "secondary",
              mtime=datetime(2026, 3, 25, 11, 0, tzinfo=timezone.utc)),
    ]
    groups = group_files_into_sessions(files)
    assert len(groups) == 1
    assert len(groups[0].files) == 3


def test_group_id_is_stable_while_the_print_is_still_being_written() -> None:
    """Regression: the id hashed the file-name SET, so every log the printer
    added mid-print produced a new id. The re-import guard (skip an existing
    group_id) then missed, inserting a duplicate session per scan."""
    day = datetime(2026, 3, 23, 8, 0, tzinfo=timezone.utc)
    main = _file("23.03.2026.log", "main_event_log", "primary", mtime=day)
    sensors = _file("23.03.2026_sensors.log", "sensors_log", "secondary", mtime=day)
    time_log = _file("23.03.2026_time.log", "time_log", "secondary", mtime=day)

    ids = [
        group_files_into_sessions([main])[0].group_id,
        group_files_into_sessions([main, sensors])[0].group_id,
        group_files_into_sessions([main, sensors, time_log])[0].group_id,
    ]
    assert len(set(ids)) == 1, f"id changed as files arrived: {ids}"


def test_two_prints_on_the_same_day_are_separate_sessions() -> None:
    day = datetime(2026, 3, 23, 8, 0, tzinfo=timezone.utc)
    files = [
        _file("23.03.2026_A.log", "main_event_log", "primary", mtime=day),
        _file("23.03.2026_A_sensors.log", "sensors_log", "secondary", mtime=day),
        _file("23.03.2026_B.log", "main_event_log", "primary", mtime=day.replace(hour=18)),
        _file("23.03.2026_B_sensors.log", "sensors_log", "secondary", mtime=day.replace(hour=18)),
    ]
    groups = group_files_into_sessions(files)
    assert len(groups) == 2
    assert [len(g.files) for g in groups] == [2, 2]


def test_reused_run_prefix_months_later_is_split_by_span_guard() -> None:
    files = [
        _file("bracket.log", "main_event_log", "primary",
              mtime=datetime(2026, 3, 1, 8, 0, tzinfo=timezone.utc)),
        _file("bracket_sensors.log", "sensors_log", "secondary",
              mtime=datetime(2026, 3, 1, 11, 0, tzinfo=timezone.utc)),
        _file("bracket_time.log", "time_log", "secondary",
              mtime=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc)),
    ]
    groups = group_files_into_sessions(files)
    assert len(groups) == 2
    assert "run_prefix_reused" in groups[1].reasons
    assert groups[0].group_id != groups[1].group_id


def test_run_prefix_extraction() -> None:
    from domain.services.session_grouping import run_prefix

    assert run_prefix("23.03.2026.log") == "23.03.2026"
    assert run_prefix("23.03.2026_sensors.log") == "23.03.2026"
    assert run_prefix("23.03.2026_Monitor100.log") == "23.03.2026"
    assert run_prefix("23.03.2026_stateFlow.log") == "23.03.2026"
    # "_stateFlowData" must not be read as "_stateFlow" + "Data"
    assert run_prefix("23.03.2026_stateFlowData.log") == "23.03.2026"
    # machine-global log, not tied to a run
    assert run_prefix("table_temp.log") is None


def test_same_print_files_stay_in_one_session() -> None:
    a = _file("a_burn.log", "burn_log", "primary", mtime=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc))
    b = _file("a_sensors.log", "sensors_log", "secondary", mtime=datetime(2026, 3, 23, 13, 5, tzinfo=timezone.utc))
    groups = group_files_into_sessions([a, b])
    assert len(groups) == 1
    assert len(groups[0].files) == 2


def test_group_id_is_deterministic_for_same_files() -> None:
    # Re-grouping the same files (any order) must yield the SAME id — this is
    # what makes re-import idempotent instead of duplicating every print.
    a = _file("23.03.2026.log", "main_event_log", "primary", mtime=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc))
    b = _file("23.03.2026_sensors.log", "sensors_log", "secondary", mtime=datetime(2026, 3, 23, 13, 1, tzinfo=timezone.utc))
    id1 = group_files_into_sessions([a, b])[0].group_id
    id2 = group_files_into_sessions([b, a])[0].group_id
    assert id1 == id2
    assert id1.startswith("session_20260323_")


def _file(name: str, family: str, role: str, mtime: datetime | None = None) -> IngestedFile:
    return IngestedFile(
        path=name,
        relative_path=name,
        classification=FileClassification(
            path=name,
            file_name=name,
            family=family,
            role=role,
            confidence=1.0,
        ),
        checksum="x",
        size_bytes=1,
        data_quality_status=DataQualityStatus.ok,
        mtime=mtime or datetime(2026, 4, 27, tzinfo=timezone.utc),
        parse_result=ParseResult(
            parser_name="test",
            parser_version="0",
            file_family=family,
            role=role,
        ),
    )


class TestLoglessFilesMakeNoSession:
    """A session is a print; a print is evidenced by the printer's own logs.

    The live DB had grown sessions built from a stray screenshot and from a
    Finder .DS_Store — both surfaced on the dashboard as prints.
    """

    def test_stray_non_log_file_makes_no_session(self):
        assert group_files_into_sessions([_file("Безымянный.png", "unsupported", "unknown")]) == []

    def test_ds_store_makes_no_session(self):
        assert group_files_into_sessions([_file(".DS_Store", "unsupported", "unknown")]) == []

    def test_real_logs_still_group(self):
        files = [
            _file("23.03.2026.log", "main_event_log", "primary"),
            _file("23.03.2026_sensors.log", "sensors_log", "secondary"),
        ]
        groups = group_files_into_sessions(files)
        assert len(groups) == 1
        assert len(groups[0].files) == 2

    def test_stray_file_dropped_without_touching_a_real_session(self):
        files = [
            _file("23.03.2026.log", "main_event_log", "primary"),
            _file("Безымянный.png", "unsupported", "unknown"),
        ]
        groups = group_files_into_sessions(files)
        assert len(groups) == 1
        assert [f.classification.file_name for f in groups[0].files] == ["23.03.2026.log"]

    def test_unsupported_file_kept_when_its_bucket_holds_a_real_log(self):
        """run_prefix() only recognises .log names, so every other file lands in
        the prefixless bucket. A print whose main log has a non-standard name
        lands there too — dropping per file rather than per bucket would strip
        its companions. (Whether they then stay in one group is up to the
        temporal split, not to this filter.)"""
        stamp = datetime(2026, 3, 23, 12, tzinfo=timezone.utc)
        files = [
            _file("printer_run.txt", "main_event_log", "primary", mtime=stamp),
            _file("Безымянный.png", "unsupported", "unknown", mtime=stamp),
        ]
        kept = {f.classification.file_name
                for group in group_files_into_sessions(files) for f in group.files}
        assert kept == {"printer_run.txt", "Безымянный.png"}

