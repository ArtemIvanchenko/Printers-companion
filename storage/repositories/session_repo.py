"""Session repository for database operations."""
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.models.sessions import BuildSession, ImportJob
from domain.services.import_jobs import ImportJobRecord
from domain.services.ingestion import IngestedFile


class SessionRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def flush(self) -> None:
        """Flush pending changes within the unit of work; the boundary commits."""
        self.db.flush()

    def save_import_job(self, job: ImportJobRecord) -> None:
        data = job.model_dump()
        values = {
            "source_path": job.source_path,
            "source_name": job.source_name,
            "source_kind": job.source_kind,
            "status": job.status.value,
            "detected_at": job.detected_at,
            "updated_at": job.updated_at,
            "confirmation_deadline": job.confirmation_deadline,
            "confirmed_by": job.confirmed_by,
            "confirmed_at": job.confirmed_at,
            "postponed_until": job.postponed_until,
            "ignored_by": job.ignored_by,
            "ignored_at": job.ignored_at,
            "last_stability_check_at": job.last_stability_check_at,
            "file_snapshot": jsonable_encoder(data["file_snapshot"]),
            "checksum_manifest": jsonable_encoder(data["checksum_manifest"]),
            "session_ids": jsonable_encoder(data["session_ids"]),
            "report_ids": jsonable_encoder(data["report_ids"]),
            "missing_context_questions": jsonable_encoder(data["missing_context_questions"]),
            "notification_log": jsonable_encoder(data["notification_log"]),
            "error": job.error,
            "audit_trail": jsonable_encoder(data["audit_trail"]),
        }
        existing = self.db.get(ImportJob, job.import_job_id)
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
        else:
            self.db.add(ImportJob(import_job_id=job.import_job_id, **values))

    def get_import_job(self, import_job_id: str) -> ImportJobRecord | None:
        row = self.db.get(ImportJob, import_job_id)
        if not row:
            return None
        return ImportJobRecord.model_validate({
            "import_job_id": row.import_job_id,
            "source_path": row.source_path,
            "source_name": row.source_name,
            "source_kind": row.source_kind,
            "status": row.status,
            "detected_at": row.detected_at,
            "updated_at": row.updated_at,
            "confirmation_deadline": row.confirmation_deadline,
            "confirmed_by": row.confirmed_by,
            "confirmed_at": row.confirmed_at,
            "postponed_until": row.postponed_until,
            "ignored_by": row.ignored_by,
            "ignored_at": row.ignored_at,
            "last_stability_check_at": row.last_stability_check_at,
            "file_snapshot": row.file_snapshot or {},
            "checksum_manifest": row.checksum_manifest or {},
            "session_ids": row.session_ids or [],
            "report_ids": row.report_ids or [],
            "missing_context_questions": row.missing_context_questions or [],
            "notification_log": row.notification_log or [],
            "error": row.error,
            "audit_trail": row.audit_trail or [],
        })

    def list_import_jobs(self) -> list[ImportJobRecord]:
        rows = self.db.scalars(select(ImportJob).order_by(ImportJob.detected_at.desc())).all()
        return [
            ImportJobRecord.model_validate({
                "import_job_id": row.import_job_id,
                "source_path": row.source_path,
                "source_name": row.source_name,
                "source_kind": row.source_kind,
                "status": row.status,
                "detected_at": row.detected_at,
                "updated_at": row.updated_at,
                "confirmation_deadline": row.confirmation_deadline,
                "confirmed_by": row.confirmed_by,
                "confirmed_at": row.confirmed_at,
                "postponed_until": row.postponed_until,
                "ignored_by": row.ignored_by,
                "ignored_at": row.ignored_at,
                "last_stability_check_at": row.last_stability_check_at,
                "file_snapshot": row.file_snapshot or {},
                "checksum_manifest": row.checksum_manifest or {},
                "session_ids": row.session_ids or [],
                "report_ids": row.report_ids or [],
                "missing_context_questions": row.missing_context_questions or [],
                "notification_log": row.notification_log or [],
                "error": row.error,
                "audit_trail": row.audit_trail or [],
            })
            for row in rows
        ]

    def save_session_payload(self, session_id: str, payload: dict[str, Any]) -> None:
        existing = self.db.get(BuildSession, session_id)
        context = {"runtime_payload": jsonable_encoder(payload)}
        if existing:
            existing.context = context
            existing.updated_at = datetime.now(timezone.utc)
        else:
            self.db.add(
                BuildSession(
                    session_id=session_id,
                    status="runtime_payload",
                    context=context,
                    grouping_confidence=float(payload.get("group", {}).get("confidence") or 0.0),
                )
            )

    def get_session_payload(self, session_id: str) -> dict[str, Any] | None:
        row = self.db.get(BuildSession, session_id)
        if not row:
            return None
        return (row.context or {}).get("runtime_payload")

    def list_session_payloads(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self.db.scalars(select(BuildSession).order_by(BuildSession.created_at.desc())).all()
        payloads = []
        for row in rows:
            payload = (row.context or {}).get("runtime_payload")
            if payload:
                payloads.append((row.session_id, payload))
        return payloads

    def get_session_files(self, session_id: str) -> list[IngestedFile] | None:
        payload = self.get_session_payload(session_id)
        if not payload:
            return None
        return [IngestedFile.model_validate(item) for item in payload.get("files", [])]