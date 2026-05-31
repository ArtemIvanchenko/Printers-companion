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
    files = [_file("a.log", "main_event_log", "primary"), _file("b.log", "burn_log", "primary")]
    group = group_files_into_sessions(files)[0]
    left, right = manual_split(group, {files[1].path})
    merged = manual_merge([left, right])
    assert left.reasons == ["manual_split"]
    assert merged.confidence == 1.0
    assert len(merged.files) == 2


def test_large_time_gap_splits_same_family_files_into_separate_sessions() -> None:
    # Two prints of identical file family, four days apart, must NOT be merged:
    # the temporal gap is decisive, family continuity must not override it.
    early = _file("23.03_burn.log", "burn_log", "primary", mtime=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc))
    late = _file("27.03_burn.log", "burn_log", "primary", mtime=datetime(2026, 3, 27, 9, 0, tzinfo=timezone.utc))
    groups = group_files_into_sessions([early, late])
    assert len(groups) == 2
    assert "new_gap_exceeded" in groups[1].reasons


def test_same_print_files_stay_in_one_session() -> None:
    a = _file("a_burn.log", "burn_log", "primary", mtime=datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc))
    b = _file("a_sensors.log", "sensors_log", "secondary", mtime=datetime(2026, 3, 23, 13, 5, tzinfo=timezone.utc))
    groups = group_files_into_sessions([a, b])
    assert len(groups) == 1
    assert len(groups[0].files) == 2


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

