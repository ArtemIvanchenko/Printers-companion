"""Atomic publication of an already calculated import.

No parser, model fit, file access or object-store upload belongs in publish_import.
The worker owns the surrounding transaction; rollback preserves the old card.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from datetime import timezone

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.prediction.layer_timings import replace_layer_timings
from analytics.prediction.timing_snapshot import MANIFEST_KEY
from core.versioning.provenance import stable_hash
from domain.enums.common import ImportJobStatus
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from domain.services.compute_affinity import require_compute_owner
from domain.services.importing.fence import ImportFence
from domain.services.importing.contracts import RetryableImportError, ImportPersistenceError

if TYPE_CHECKING:
    from domain.services.importing.contracts import ImportExecutionResult


class ImportPublicationConflict(RetryableImportError):
    """Another import changed the session after this attempt captured its base."""


def session_publication_token(session: BuildSession) -> str:
    updated_at = session.updated_at
    # SQLite drops tzinfo on round-trip, PostgreSQL retains it.
    if updated_at is not None:
        updated_at = (
            updated_at.replace(tzinfo=timezone.utc)
            if updated_at.tzinfo is None
            else updated_at.astimezone(timezone.utc)
        )
    return stable_hash({"context": session.context or {}, "updated_at": updated_at})


def prepare_import_reports(result: ImportExecutionResult) -> dict[str, dict]:
    """Upload immutable report artifacts before entering final publication SQL."""
    from storage.repositories.runtime import _offload_report, _report_payload, _sanitize_for_json

    from core.config.settings import get_settings

    prepared = {}
    for report_id, report in result.reports.items():
        uri = _offload_report(report_id, report)
        if uri is None and get_settings().app_env != "test":
            raise ImportPersistenceError("Полный отчёт не сохранён на NAS. Импорт будет повторён.")
        prepared[report_id] = {
            "storage_uri": uri,
            "payload": _sanitize_for_json(jsonable_encoder(_report_payload(report))),
            "version_metadata": jsonable_encoder(report.get("version_metadata", {})),
        }
    return prepared


def publish_import(
    db: Session,
    result: ImportExecutionResult,
    *,
    fence: ImportFence,
    prepared_reports: dict[str, dict],
) -> None:
    """Validate the whole attempt, then publish rows, overview and completion."""
    from analytics.prediction.calibration import enqueue_calibration
    from analytics.prediction.retraining import enqueue_retraining_for_session
    from domain.services.print_linking import auto_link_print_records
    from storage.repositories.prints_repo import PrintsRepository
    from storage.repositories.runtime import RuntimeRepository

    current = fence.verify(db)
    if current.print_record_id:
        result.job.print_record_id = current.print_record_id
    repo = RuntimeRepository(db)
    if result.job.status != ImportJobStatus.done:
        if result.sessions or result.reports or result.layer_timings:
            raise ValueError("Незавершённый импорт не может публиковать анализ")
        repo.save_notifications(result.notifications)
        fence.verify(db)
        result.job.lease_owner = None
        result.job.lease_until = None
        repo.save_import_job(result.job)
        return
    if (
        set(result.sessions) != set(result.layer_timings)
        or set(result.sessions) != set(result.previous_session_tokens)
        or set(result.reports) != set(prepared_reports)
    ):
        raise ValueError("Импорт не подготовил полный набор слоёв, сводок и отчётов")
    if set(result.job.session_ids) != set(result.sessions) or set(result.job.report_ids) != set(
        result.reports
    ):
        raise ValueError("Список сессий задания не соответствует подготовленным результатам")
    if {report.get("session_id") for report in result.reports.values()} != set(result.sessions):
        raise ValueError("Отчёты не покрывают все подготовленные сессии")

    # Same parent lock order as calibration. A second job for this session
    # must re-prepare if a newer result has already replaced its captured base.
    for sid in sorted(result.sessions):
        session = db.scalar(
            select(BuildSession)
            .where(BuildSession.session_id == sid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if session is None:
            raise ImportPublicationConflict("Сессия исчезла во время расчёта импорта")
        require_compute_owner(
            entity_type="session",
            entity_id=sid,
            origin_compute_node_id=session.origin_compute_node_id,
            requested_compute_node_id=fence.owner_node_id,
        )
        if session_publication_token(session) != result.previous_session_tokens[sid]:
            raise ImportPublicationConflict(
                "Сессия уже изменена другим импортом; нужен повторный разбор."
            )
        prepared = result.layer_timings[sid]
        if prepared.manifest.get("source") != fence.as_dict():
            raise ValueError("Слои принадлежат другому поколению импорта")
        if (result.sessions[sid].get("group") or {}).get(
            "timing_publication_id"
        ) != prepared.manifest.get("publication_id"):
            raise ValueError("Сводка и слои принадлежат разным результатам")
    if result.job.print_record_id:
        record = db.get(PrintRecord, result.job.print_record_id)
        if record is None:
            raise ValueError("Карточка импорта больше не существует")
        require_compute_owner(
            entity_type="print_record",
            entity_id=record.record_id,
            origin_compute_node_id=record.origin_compute_node_id,
            requested_compute_node_id=fence.owner_node_id,
        )
    # Locks may have taken time. Check fresh lease BEFORE replacing any facts.
    fence.verify(db)
    repo.save_sessions(result.sessions, origin_compute_node_id=fence.owner_node_id)
    for sid, prepared in result.layer_timings.items():
        replace_layer_timings(sid, prepared, db)
    for report_id, report in result.reports.items():
        prepared = result.layer_timings.get(report.get("session_id"))
        payload = prepared_reports[report_id]["payload"]
        snapshot = (result.sessions.get(report.get("session_id"), {}).get("group") or {}).get("analysis_snapshot")
        if (report.get("analysis_snapshot") != snapshot
                or payload.get("analysis_snapshot") != snapshot):
            raise ValueError("Отчёт и сводка принадлежат разным аналитическим снимкам")
        if (
            prepared is None
            or report.get("timing_publication") != prepared.manifest
            or payload.get("timing_publication") != prepared.manifest
            or payload.get("report_id") != report_id
            or payload.get("session_id") != report.get("session_id")
        ):
            raise ValueError("Отчёт и слои принадлежат разным результатам")
        repo.save_prepared_report(report_id, prepared_reports[report_id])
    repo.save_notifications(result.notifications)

    links = []
    if result.job.print_record_id and len(result.job.session_ids) == 1:
        sid = result.job.session_ids[0]
        session = db.get(BuildSession, sid)
        if PrintsRepository(db).link_session(
            result.job.print_record_id,
            sid,
            session.start_ts if session else None,
            compute_node_id=fence.owner_node_id,
            link_evidence={
                "method": "operator_card_upload",
                "import_job_id": fence.import_job_id,
                "eligible": True,
                "auto_link_allowed": True,
            },
        ):
            links.append({"session_id": sid})
    links.extend(auto_link_print_records(db, origin_compute_node_id=fence.owner_node_id))
    for sid in {str(link["session_id"]) for link in links if link.get("session_id")}:
        enqueue_retraining_for_session(db, sid)
    if result.job.session_ids or links:
        enqueue_calibration(
            db,
            owner_node_id=fence.owner_node_id,
            trigger="import_completed",
            event_id=f"import:{fence.import_job_id}:{fence.lease_generation}",
        )
    # Including late TTL expiry or a write failure must roll back EVERY output.
    fence.verify(db)
    result.job.lease_owner = None
    result.job.lease_until = None
    repo.save_import_job(result.job)


__all__ = ["publish_import", "prepare_import_reports", "session_publication_token", "MANIFEST_KEY"]
