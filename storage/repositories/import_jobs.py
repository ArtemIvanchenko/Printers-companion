"""SQL-only import detection, reads and owner-local leases.

This repository never hashes/opens source logs or contacts MinIO. It operates
inside the caller's existing unit of work: no implicit commit or Session close.
Callers use this repository directly rather than going through a runtime facade.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.orm import Session

from domain.models.entities import ImportJob
from domain.services.importing.contracts import ImportJobRecord


class ImportJobsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def save_import_job(self, job: ImportJobRecord) -> None:
        data = job.model_dump(mode="json")
        values = {
            "owner_node_id": job.owner_node_id,
            "print_record_id": job.print_record_id,
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
            "lease_owner": job.lease_owner,
            "lease_until": job.lease_until,
            "lease_generation": job.lease_generation,
            "ignored_by": job.ignored_by,
            "ignored_at": job.ignored_at,
            "last_stability_check_at": job.last_stability_check_at,
            "stability_check_attempts": job.stability_check_attempts,
            "file_snapshot": data["file_snapshot"],
            "checksum_manifest": data["checksum_manifest"],
            "source_objects": data["source_objects"],
            "session_ids": data["session_ids"],
            "report_ids": data["report_ids"],
            "missing_context_questions": data["missing_context_questions"],
            "notification_log": data["notification_log"],
            "error": job.error,
            "audit_trail": data["audit_trail"],
        }
        row = self.db.get(ImportJob, job.import_job_id)
        if row is None:
            self.db.add(ImportJob(import_job_id=job.import_job_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)

    def get_import_job(self, import_job_id: str) -> ImportJobRecord | None:
        row = self.db.get(ImportJob, import_job_id)
        return _import_job_record_from_row(row) if row else None

    def get_import_job_for_update(self, import_job_id: str) -> ImportJobRecord | None:
        """Read and lock an import row for an operator state transition."""
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.import_job_id == import_job_id)
            .with_for_update()
        )
        return _import_job_record_from_row(row) if row else None

    def lock_import_candidate(self, owner_node_id: str, source_path: str) -> None:
        """Serialize detection of the same local batch on PostgreSQL.

        Watcher and browser upload can report one freshly-created directory at
        the same time. A transaction-scoped advisory lock closes the list→add
        race without imposing a global unique constraint that would prevent a
        legitimately changed file from being re-imported at the same path.
        SQLite tests are single-process and need no equivalent.
        """
        if self.db.get_bind().dialect.name != "postgresql":
            return
        digest = hashlib.sha256(
            f"{owner_node_id}\0{source_path}".encode("utf-8")
        ).digest()
        lock_key = int.from_bytes(digest[:8], "big", signed=True)
        self.db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )

    def list_import_jobs(
        self,
        owner_node_id: str | None = None,
        *,
        skip: int = 0,
        limit: int | None = None,
    ) -> list[ImportJobRecord]:
        query = select(ImportJob)
        if owner_node_id is not None:
            query = query.where(ImportJob.owner_node_id == owner_node_id)
        query = query.order_by(ImportJob.detected_at.desc()).offset(max(0, skip))
        if limit is not None:
            query = query.limit(max(0, limit))
        rows = self.db.scalars(query).all()
        return [_import_job_record_from_row(row) for row in rows]

    def list_import_jobs_by_source_path(
        self,
        *,
        owner_node_id: str,
        source_path: str,
    ) -> list[ImportJobRecord]:
        """Only versions of one local path, newest first."""
        rows = self.db.scalars(
            select(ImportJob)
            .where(
                ImportJob.owner_node_id == owner_node_id,
                ImportJob.source_path == source_path,
            )
            .order_by(ImportJob.detected_at.desc())
        ).all()
        return [_import_job_record_from_row(row) for row in rows]

    def has_terminal_import_job_by_name(
        self,
        *,
        owner_node_id: str,
        source_name: str,
    ) -> bool:
        """Cheap hint used before any potentially long local hashing."""
        found = self.db.scalar(
            select(ImportJob.import_job_id)
            .where(
                ImportJob.owner_node_id == owner_node_id,
                ImportJob.source_name == source_name,
                ImportJob.status.in_(("done", "needs_operator_context")),
            )
            .limit(1)
        )
        return found is not None

    def list_terminal_import_jobs_by_name(
        self,
        *,
        owner_node_id: str,
        source_name: str,
    ) -> list[ImportJobRecord]:
        """Completed imports that may have the same content at another path."""
        rows = self.db.scalars(
            select(ImportJob)
            .where(
                ImportJob.owner_node_id == owner_node_id,
                ImportJob.source_name == source_name,
                ImportJob.status.in_(("done", "needs_operator_context")),
            )
            .order_by(ImportJob.detected_at.desc())
        ).all()
        return [_import_job_record_from_row(row) for row in rows]

    def count_import_jobs(self, owner_node_id: str | None = None) -> int:
        query = select(func.count()).select_from(ImportJob)
        if owner_node_id is not None:
            query = query.where(ImportJob.owner_node_id == owner_node_id)
        return int(self.db.scalar(query) or 0)

    def latest_import_job(self, owner_node_id: str) -> ImportJobRecord | None:
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.owner_node_id == owner_node_id)
            .order_by(ImportJob.updated_at.desc())
            .limit(1)
        )
        return _import_job_record_from_row(row) if row else None

    def claim_next_import_job(
        self,
        *,
        owner_node_id: str,
        lease_owner: str,
        now: datetime | None = None,
        lease_seconds: int = 900,
    ) -> ImportJobRecord | None:
        """Atomically lease one due import owned by this operator PC.

        ``owner_node_id`` is stable across container restarts; ``lease_owner``
        identifies this particular worker process.  Other PCs cannot see the
        row through this claim even though the database itself is shared.
        """
        now = now or datetime.now(timezone.utc)
        due = or_(
            ImportJob.status == "checking_stability",
            and_(
                ImportJob.status == "postponed",
                ImportJob.confirmed_by.is_not(None),
                ImportJob.postponed_until.is_not(None),
                ImportJob.postponed_until <= now,
            ),
        )
        lease_available = or_(
            ImportJob.lease_until.is_(None),
            ImportJob.lease_until < now,
        )
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.owner_node_id == owner_node_id, due, lease_available)
            .order_by(ImportJob.updated_at, ImportJob.detected_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        row.lease_owner = lease_owner
        row.lease_until = now + timedelta(seconds=max(60, lease_seconds))
        row.lease_generation += 1
        row.updated_at = now
        self.db.flush()
        return _import_job_record_from_row(row)

    def renew_import_job_lease(
        self,
        import_job_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> bool:
        """Keep a long local parse leased without holding a NAS transaction.

        Renewal is fenced by both the worker-process identity and generation.
        It never revives an expired lease, which could already have been
        reclaimed by a restarted worker on the same operator PC.
        """
        now = now or datetime.now(timezone.utc)
        row = self.db.scalar(
            select(ImportJob)
            .where(ImportJob.import_job_id == import_job_id)
            .with_for_update()
        )
        if row is None or row.lease_owner != lease_owner:
            return False
        if row.lease_generation != lease_generation or row.lease_until is None:
            return False
        lease_until = row.lease_until
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if lease_until <= now:
            return False
        row.lease_until = now + timedelta(seconds=max(60, lease_seconds))
        row.updated_at = now
        self.db.flush()
        return True


_IMPORT_JOB_SCALARS = (
    "import_job_id", "owner_node_id", "print_record_id", "source_path", "source_name", "source_kind", "status",
    "detected_at", "updated_at", "confirmation_deadline", "confirmed_by",
    "confirmed_at", "postponed_until", "lease_owner", "lease_until", "lease_generation",
    "ignored_by", "ignored_at",
    "last_stability_check_at", "stability_check_attempts", "error",
)
_IMPORT_JOB_DICTS = ("file_snapshot", "checksum_manifest", "source_objects")
_IMPORT_JOB_LISTS = (
    "session_ids", "report_ids", "missing_context_questions",
    "notification_log", "audit_trail",
)


def _import_job_record_from_row(row: ImportJob) -> ImportJobRecord:
    """Rebuild an ImportJobRecord from its ORM row (shared by get + list)."""
    data: dict[str, Any] = {f: getattr(row, f) for f in _IMPORT_JOB_SCALARS}
    data.update({f: getattr(row, f) or {} for f in _IMPORT_JOB_DICTS})
    data.update({f: getattr(row, f) or [] for f in _IMPORT_JOB_LISTS})
    return ImportJobRecord.model_validate(data)
