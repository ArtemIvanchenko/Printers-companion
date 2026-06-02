from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from core.config.settings import Settings
from domain.enums.common import (
    DataQualityStatus,
    FileRole,
    ImportJobStatus,
    SourceFileFamily,
)
from domain.services.ingestion import IngestedFile
from domain.schemas.parsing import (
    CanonicalEventDraft,
    FileClassification,
    ParseResult,
    SourceLocation,
)
from domain.services.import_jobs import (
    confirm_import_job,
    detect_import_candidate,
    persist_parse_results_to_db,
)
from profiles.m350.profile import build_registry, get_profile


def test_detected_import_waits_for_operator_confirmation(tmp_path: Path) -> None:
    folder = tmp_path / "incoming" / "logs_001"
    folder.mkdir(parents=True)
    (folder / "job.log").write_text("2026-04-27 10:00:00 Старт печати\n", encoding="utf-8")
    settings = Settings(
        require_operator_import_confirmation=True,
        import_confirmation_timeout_hours=24,
    )

    result = detect_import_candidate(folder, settings=settings)

    assert result.job.status == ImportJobStatus.awaiting_operator_confirmation
    assert result.job.session_ids == []
    assert result.job.report_ids == []
    assert result.notifications[0].text == "Найдена новая папка логов: logs_001. Начать импорт?"
    assert [button.text for button in result.notifications[0].buttons] == [
        "Импортировать",
        "Игнорировать",
        "Проверить позже",
    ]


def test_confirm_defers_when_files_are_still_changing(tmp_path: Path) -> None:
    folder = tmp_path / "incoming" / "logs_002"
    folder.mkdir(parents=True)
    (folder / "job.log").write_text("2026-04-27 10:00:00 Старт печати\n", encoding="utf-8")
    now = datetime.now(timezone.utc)
    settings = Settings(file_stability_seconds=60, file_stability_retry_seconds=30)
    job = detect_import_candidate(folder, settings=settings, now=now).job

    result = confirm_import_job(job, registry=build_registry(), profile=get_profile(), settings=settings, now=now)

    assert result.job.status == ImportJobStatus.postponed
    assert result.notifications[0].text == "Файлы еще копируются. Повторю проверку через 30 секунд."
    assert result.job.session_ids == []


def test_confirm_runs_import_analysis_and_report_after_stability(tmp_path: Path) -> None:
    folder = tmp_path / "incoming" / "logs_003"
    folder.mkdir(parents=True)
    log = folder / "job.log"
    log.write_text("2026-04-27 10:00:00 Старт печати\n2026-04-27 10:01:00 слой 1\n", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp()
    log.touch()
    import os

    os.utime(log, (old, old))
    settings = Settings(file_stability_seconds=0, file_stability_retry_seconds=30)
    job = detect_import_candidate(folder, settings=settings).job

    result = confirm_import_job(job, registry=build_registry(), profile=get_profile(), settings=settings)

    assert result.job.status == ImportJobStatus.needs_operator_context
    assert result.job.session_ids
    assert result.job.report_ids
    assert result.reports[result.job.report_ids[0]]["markdown"].startswith("# Session Report")
    assert result.notifications[0].metadata["kind"] == "import_summary"


def test_persist_parse_results_stores_event_provenance(tmp_path: Path) -> None:
    """Source location (line/offset/excerpt) lives on the nested
    CanonicalEventDraft.source and must be persisted to canonical_events."""
    from domain.models.entities import BuildSession
    from storage.db.session import SessionLocal
    from storage.repositories.runtime import RuntimeRepository

    session_id = f"sess_{uuid4().hex}"
    with SessionLocal() as db:
        db.add(BuildSession(
            session_id=session_id,
            status="import_processing",
            classification="INCOMPLETE_OR_UNKNOWN",
            classification_confidence=0.0,
            grouping_confidence=0.0,
        ))
        db.commit()

    log_path = tmp_path / "job.log"
    log_path.write_text("2026-04-27 10:00:00 Старт печати\n", encoding="utf-8")

    event = CanonicalEventDraft(
        ts=datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc),
        raw_timestamp="2026-04-27 10:00:00",
        event_type="print_start",
        source=SourceLocation(
            source_line=42,
            source_offset=128,
            raw_excerpt="2026-04-27 10:00:00 Старт печати",
        ),
    )
    ingested = IngestedFile(
        path=str(log_path),
        relative_path="job.log",
        classification=FileClassification(
            path=str(log_path),
            file_name="job.log",
            family=SourceFileFamily.main_event_log,
            role=FileRole.primary,
            confidence=1.0,
        ),
        checksum="deadbeef",
        size_bytes=log_path.stat().st_size,
        encoding="utf-8",
        data_quality_status=DataQualityStatus.ok,
        mtime=datetime.now(timezone.utc),
        parse_result=ParseResult(
            parser_name="test",
            parser_version="1.0",
            file_family=SourceFileFamily.main_event_log,
            role=FileRole.primary,
            events=[event],
        ),
    )

    files_saved, events_saved = persist_parse_results_to_db(session_id, [ingested])

    assert files_saved == 1
    assert events_saved == 1

    with SessionLocal() as db:
        stored = RuntimeRepository(db).list_canonical_events_by_session(session_id)

    assert len(stored) == 1
    assert stored[0].source_line == 42
    assert stored[0].source_offset == 128
    assert stored[0].raw_excerpt == "2026-04-27 10:00:00 Старт печати"

