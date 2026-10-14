"""Local worker task for guarded ML retraining."""

from __future__ import annotations

import logging
import sqlite3

from analytics.prediction.retraining import (
    JOB_TYPE,
    RetrainingInputsChanged,
    StaleModelLease,
    calculate_retraining,
    prepare_retraining,
    publish_retraining,
    retraining_input_identity,
)
from core.config.settings import get_settings
from storage.db.session import is_database_unavailable, session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker.lease_heartbeat import LeaseHeartbeat

logger = logging.getLogger(__name__)


def _defer_model_task(
    job_id: str,
    error: Exception,
    *,
    lease_owner: str,
    lease_generation: int,
) -> None:
    try:
        with session_scope() as db:
            JobsRepository(db).defer_retry(
                job_id,
                str(error),
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
    except Exception:
        # PostgreSQL still being down is expected here. The existing fenced
        # lease leaves the job reclaimable by this same workstation later.
        logger.exception("could not defer model task %s", job_id)


def process_next_model_task(lease_owner: str, owner_node_id: str | None = None) -> bool:
    """Claim, train locally, then atomically publish one model result."""
    settings = get_settings()
    owner_node_id = owner_node_id or settings.compute_node_id
    with session_scope() as db:
        job = JobsRepository(db).claim_next(
            JOB_TYPE,
            owner_node_id=owner_node_id,
            lease_owner=lease_owner,
            lease_seconds=settings.job_lease_seconds,
        )
    if job is None:
        return False

    job_id = str(job["job_id"])
    lease_generation = int(job["lease_generation"])
    try:
        if (
            job["owner_node_id"] != owner_node_id
            or (job.get("payload") or {}).get("owner_node_id") != owner_node_id
        ):
            raise RuntimeError("model task owner does not match this workstation")

        # Copy compact session features/model artifacts in a short transaction.
        with session_scope() as db:
            prepared = prepare_retraining(db)
        expected_inputs = retraining_input_identity(prepared)

        def renew_lease() -> bool:
            with session_scope() as heartbeat_db:
                return JobsRepository(heartbeat_db).renew_lease(
                    job_id,
                    lease_owner=lease_owner,
                    lease_generation=lease_generation,
                    lease_seconds=settings.job_lease_seconds,
                )

        # All cross-validation/LightGBM work happens here, off the NAS and with
        # no database connection held open.
        with LeaseHeartbeat(
            renew_lease,
            description=f"model-retrain:{job_id}",
            interval_seconds=settings.job_heartbeat_seconds,
        ) as heartbeat:
            calculated = calculate_retraining(prepared, owner_node_id=owner_node_id)
        if heartbeat.lost:
            logger.warning("discarded model result after losing lease for %s", job_id)
            return True

        with session_scope() as db:
            publish_retraining(
                db, calculated, expected_inputs=expected_inputs, job_id=job_id,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
        logger.info("model task %s completed on %s", job_id, owner_node_id)
    except StaleModelLease:
        logger.warning("discarded model publication after losing lease for %s", job_id)
    except Exception as exc:
        original = getattr(exc, "orig", None)
        lock_busy = (getattr(original, "sqlstate", None) == "55P03" or (
            isinstance(original, sqlite3.OperationalError)
            and getattr(original, "sqlite_errorcode", 0) & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        ))
        if isinstance(exc, RetrainingInputsChanged) or lock_busy or is_database_unavailable(exc):
            _defer_model_task(
                job_id,
                exc,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
            logger.warning("model task %s postponed: %s", job_id, exc)
        else:
            with session_scope() as db:
                JobsRepository(db).fail(
                    job_id,
                    str(exc),
                    retryable=True,
                    lease_owner=lease_owner,
                    lease_generation=lease_generation,
                )
            logger.exception("model task %s failed", job_id)
    return True


__all__ = ["process_next_model_task"]
