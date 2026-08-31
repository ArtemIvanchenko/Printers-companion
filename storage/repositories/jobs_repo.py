"""Atomic persistence and leasing for restart-safe background jobs."""

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from domain.models.jobs import BackgroundJob


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_dict(row: BackgroundJob) -> dict[str, Any]:
    return {
        "job_id": row.job_id,
        "job_type": row.job_type,
        "owner_node_id": row.owner_node_id,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "idempotency_key": row.idempotency_key,
        "status": row.status,
        "payload": row.payload_json or {},
        "result": row.result_json or {},
        "error": row.error,
        "attempts": row.attempts,
        "max_attempts": row.max_attempts,
        "available_at": row.available_at.isoformat() if row.available_at else None,
        "lease_owner": row.lease_owner,
        "lease_until": row.lease_until.isoformat() if row.lease_until else None,
        "lease_generation": row.lease_generation,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


class JobsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def enqueue(
        self,
        *,
        job_type: str,
        owner_node_id: str,
        entity_type: str,
        entity_id: str,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        existing = self.db.scalar(select(BackgroundJob).where(
            BackgroundJob.idempotency_key == idempotency_key,
        ))
        if existing is not None:
            return _as_dict(existing)
        row = BackgroundJob(
            job_type=job_type,
            owner_node_id=owner_node_id,
            entity_type=entity_type,
            entity_id=entity_id,
            idempotency_key=idempotency_key,
            payload_json=payload or {},
            max_attempts=max(1, max_attempts),
            available_at=_now(),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            # Another API worker won the same idempotency-key race.
            existing = self.db.scalar(select(BackgroundJob).where(
                BackgroundJob.idempotency_key == idempotency_key,
            ))
            if existing is None:  # pragma: no cover - defensive DB corruption guard
                raise
            return _as_dict(existing)
        return _as_dict(row)

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self.db.get(BackgroundJob, job_id)
        return _as_dict(row) if row else None

    def claim_next(
        self,
        job_type: str,
        *,
        owner_node_id: str,
        lease_owner: str,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        now = now or _now()
        row = self.db.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.job_type == job_type)
            .where(BackgroundJob.owner_node_id == owner_node_id)
            .where(or_(
                and_(
                    BackgroundJob.status == "pending",
                    BackgroundJob.available_at <= now,
                    BackgroundJob.attempts < BackgroundJob.max_attempts,
                ),
                (BackgroundJob.status == "running") & (BackgroundJob.lease_until < now),
            ))
            .order_by(BackgroundJob.available_at, BackgroundJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        if row.status == "running" and row.attempts >= row.max_attempts:
            # A process died during its final allowed attempt. There is no
            # future claimant that could call fail(), so close the orphan here
            # instead of leaving a permanently "running" row.
            row.status = "failed"
            row.error = "Worker lease expired during the final allowed attempt"
            row.lease_owner = None
            row.lease_until = None
            row.finished_at = now
            row.updated_at = now
            self.db.flush()
            return None
        row.status = "running"
        row.attempts += 1
        row.lease_owner = lease_owner
        row.lease_until = now + timedelta(seconds=max(60, lease_seconds))
        row.lease_generation += 1
        row.started_at = row.started_at or now
        row.updated_at = now
        row.error = None
        self.db.flush()
        return _as_dict(row)

    def complete(
        self,
        job_id: str,
        result: dict[str, Any] | None = None,
        *,
        lease_owner: str,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        # Lock before checking the fence. Without this, a reclaiming worker can
        # commit a newer generation between our plain read and UPDATE, after
        # which this stale ORM instance would overwrite the fresh lease.
        row = self.db.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.job_id == job_id)
            .with_for_update()
        )
        if row is None:
            return None
        now = _now()
        if row.lease_owner != lease_owner:
            return None
        if row.lease_generation != lease_generation:
            return None
        lease_until = row.lease_until
        if lease_until is None:
            return None
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if lease_until <= now:
            return None
        row.status = "done"
        row.result_json = result or {}
        row.error = None
        row.lease_owner = None
        row.lease_until = None
        row.finished_at = now
        row.updated_at = now
        self.db.flush()
        return _as_dict(row)

    def renew_lease(
        self,
        job_id: str,
        *,
        lease_owner: str,
        lease_generation: int,
        lease_seconds: int = 900,
        now: datetime | None = None,
    ) -> bool:
        """Extend only the still-current lease for a long local calculation.

        The row lock makes renewal and crash-reclaim mutually exclusive.  An
        already expired lease is deliberately not resurrected: once it becomes
        claimable, only a fresh generation may own the job.
        """
        now = now or _now()
        row = self.db.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.job_id == job_id)
            .with_for_update()
        )
        if row is None or row.status != "running":
            return False
        if row.lease_owner != lease_owner or row.lease_generation != lease_generation:
            return False
        if row.lease_until is None:
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

    def fail(
        self,
        job_id: str,
        error: str,
        *,
        retryable: bool = True,
        lease_owner: str,
        lease_generation: int,
    ) -> dict[str, Any] | None:
        row = self.db.scalar(
            select(BackgroundJob)
            .where(BackgroundJob.job_id == job_id)
            .with_for_update()
        )
        if row is None:
            return None
        now = _now()
        if row.lease_owner != lease_owner:
            return None
        if row.lease_generation != lease_generation:
            return None
        lease_until = row.lease_until
        if lease_until is None:
            return None
        if lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if lease_until <= now:
            return None
        should_retry = retryable and row.attempts < row.max_attempts
        row.status = "pending" if should_retry else "failed"
        row.error = error[:4000]
        row.available_at = now + timedelta(seconds=min(300, 5 * 2 ** max(0, row.attempts - 1)))
        row.lease_owner = None
        row.lease_until = None
        row.finished_at = None if should_retry else now
        row.updated_at = now
        self.db.flush()
        return _as_dict(row)
