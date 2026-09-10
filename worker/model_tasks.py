"""Local worker task for guarded ML retraining."""

from __future__ import annotations

import logging

from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from analytics.prediction.retraining import (
    JOB_TYPE,
    calculate_retraining,
    persist_retraining,
    prepare_retraining,
)
from core.config.settings import get_settings
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker.lease_heartbeat import LeaseHeartbeat

logger = logging.getLogger(__name__)


def _defer_infrastructure_failure(
    job_id: str,
    error: Exception,
    *,
    lease_owner: str,
    lease_generation: int,
) -> None:
    try:
        with session_scope() as db:
            JobsRepository(db).defer_infrastructure(
                job_id,
                str(error),
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
    except Exception:
        # PostgreSQL still being down is expected here. The existing fenced
        # lease leaves the job reclaimable by this same workstation later.
        logger.exception("could not defer model infrastructure failure for %s", job_id)


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
            jobs = JobsRepository(db)
            completed = jobs.complete(
                job_id,
                {"model_name": calculated["model_name"], "label_count": calculated["label_count"]},
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
            if completed is None:
                logger.warning("discarded stale model completion for %s", job_id)
                return True
            published = persist_retraining(db, calculated)
            # Keep the useful decision in the durable job result as well.
            from domain.models.jobs import BackgroundJob

            row = db.get(BackgroundJob, job_id)
            if row is not None:
                row.result_json = published
        logger.info("model task %s completed on %s", job_id, owner_node_id)
    except (OperationalError, SQLAlchemyTimeoutError) as exc:
        _defer_infrastructure_failure(
            job_id,
            exc,
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )
        logger.warning("model task %s postponed after NAS database outage", job_id)
    except Exception as exc:
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
