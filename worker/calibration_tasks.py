"""Durable workstation-owned calibration, with short fenced publication."""

from __future__ import annotations

import logging

from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from analytics.prediction.calibration import JOB_TYPE, calculate_calibration, publish_calibration
from analytics.prediction.calibration_inputs import load_calibration_inputs
from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker.lease_heartbeat import LeaseHeartbeat

logger = logging.getLogger(__name__)


def process_next_calibration_task(lease_owner: str, owner_node_id: str | None = None) -> bool:
    settings = get_settings()
    owner_node_id = owner_node_id or settings.compute_node_id
    with session_scope() as db:
        job = JobsRepository(db).claim_next(
            JOB_TYPE, owner_node_id=owner_node_id, lease_owner=lease_owner,
            lease_seconds=settings.job_lease_seconds,
        )
    if job is None:
        return False
    job_id = job["job_id"]
    fence = {"lease_owner": lease_owner, "lease_generation": job["lease_generation"]}

    def renew() -> bool:
        with session_scope() as db:
            return JobsRepository(db).renew_lease(job_id, **fence, lease_seconds=settings.job_lease_seconds)

    try:
        if job["owner_node_id"] != owner_node_id or job["payload"].get("owner_node_id") != owner_node_id:
            raise RuntimeError("Владелец калибровки не совпадает с этим ПК.")
        with LeaseHeartbeat(renew, description=f"calibration:{job_id}",
                            interval_seconds=settings.job_heartbeat_seconds) as heartbeat:
            with session_scope() as db:
                inputs = load_calibration_inputs(db)
            calculated = calculate_calibration(inputs, owner_node_id=owner_node_id)
        if heartbeat.lost:
            logger.warning("discarded calibration after losing lease for %s", job_id)
            return True
        with session_scope() as db:
            result = publish_calibration(db, calculated, job_id=job_id, **fence)
            if result is None:
                logger.warning("discarded stale calibration completion for %s", job_id)
                return True
            # Completion, parameter maps and diagnostics commit together.
            db.get(BackgroundJob, job_id).result_json = result
        logger.info("calibration task %s completed on %s", job_id, owner_node_id)
    except (OperationalError, SQLAlchemyTimeoutError) as exc:
        try:
            with session_scope() as db:
                JobsRepository(db).defer_infrastructure(job_id, str(exc), **fence)
        except Exception:
            logger.exception("could not defer calibration outage for %s", job_id)
        logger.warning("calibration %s postponed after NAS outage", job_id)
    except Exception as exc:
        with session_scope() as db:
            JobsRepository(db).fail(job_id, str(exc), retryable=True, **fence)
        logger.exception("calibration %s failed; result not published", job_id)
    return True
