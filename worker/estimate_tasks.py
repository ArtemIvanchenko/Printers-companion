"""Dedicated single-owner worker for durable, CPU-heavy plate estimates."""

import logging
import signal
import time

from fastapi import HTTPException

from core.compute_identity import process_lease_owner, register_compute_node
from core.config.settings import get_settings
from core.logging.config import configure_logging
from core.preflight import exit_on_failure, run_preflight
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker.lease_heartbeat import LeaseHeartbeat

logger = logging.getLogger(__name__)
JOB_TYPE = "print_estimate"


class _DetachedGeometryCache:
    """DB-backed cache whose short transactions never span local slicing."""

    def get_geometry_cache(self, cache_key: str):
        from storage.repositories.prints_repo import PrintsRepository

        with session_scope() as db:
            return PrintsRepository(db).get_geometry_cache(cache_key)

    def save_geometry_cache(self, cache_key: str, series_json: dict, body_count: int) -> None:
        from storage.repositories.prints_repo import PrintsRepository

        with session_scope() as db:
            PrintsRepository(db).save_geometry_cache(cache_key, series_json, body_count)


def process_next_estimate(lease_owner: str, owner_node_id: str | None = None) -> bool:
    """Claim and execute one job. The lease makes a crashed job reclaimable."""
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

    job_id = job["job_id"]
    lease_generation = int(job["lease_generation"])
    record_id = str(job["payload"]["record_id"])
    try:
        payload_owner = str(job["payload"].get("owner_node_id") or "")
        if job["owner_node_id"] != owner_node_id or payload_owner != job["owner_node_id"]:
            raise HTTPException(
                409,
                "estimate job owner does not match worker/payload owner",
            )
        # Lazy import keeps the worker's startup/health path cheap.
        from api.routes.prints import (
            _calculate_prediction_snapshot,
            _enrich_prediction_interval,
            _prepare_prediction_inputs,
            _store_prediction_snapshot,
        )
        from storage.repositories.prints_repo import PrintsRepository

        def renew_lease() -> bool:
            with session_scope() as heartbeat_db:
                return JobsRepository(heartbeat_db).renew_lease(
                    job_id,
                    lease_owner=lease_owner,
                    lease_generation=lease_generation,
                    lease_seconds=settings.job_lease_seconds,
                )

        with LeaseHeartbeat(
            renew_lease,
            description=f"estimate:{job_id}",
            interval_seconds=settings.job_heartbeat_seconds,
        ) as heartbeat:
            # Short read transaction: copy only the record, parameters and
            # object references needed for this exact revision.
            with session_scope() as db:
                prepared = _prepare_prediction_inputs(
                    PrintsRepository(db),
                    record_id,
                    compute_node_id=job["owner_node_id"],
                )

            # MinIO download, mesh slicing, physics and ML all run locally with
            # no PostgreSQL connection checked out from the NAS.
            snapshot = _calculate_prediction_snapshot(
                prepared,
                geometry_cache=_DetachedGeometryCache(),
                computed_by=owner_node_id,
            )
        if heartbeat.lost:
            logger.warning("discarded calculation after losing lease for %s", job_id)
            return True

        # One short, atomic finalization transaction.  The lease fence is
        # checked *before* the card is changed and committed together with the
        # prediction.  A process whose lease expired therefore cannot write a
        # stale result and only then discover that it lost ownership.
        with session_scope() as db:
            repo = PrintsRepository(db)
            _enrich_prediction_interval(snapshot, db)
            completed = JobsRepository(db).complete(
                job_id,
                {"record_id": record_id, "prediction": snapshot},
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
            if completed is None:
                logger.warning("discarded stale completion for estimate job %s", job_id)
                return True
            _store_prediction_snapshot(
                repo,
                record_id,
                snapshot,
                expected_revision=int(prepared["record"]["revision"]),
                compute_node_id=job["owner_node_id"],
            )
        logger.info("estimate job %s completed for %s", job_id, record_id)
    except HTTPException as exc:
        with session_scope() as db:
            JobsRepository(db).fail(
                job_id,
                str(exc.detail),
                retryable=exc.status_code >= 500,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
        logger.warning("estimate job %s rejected: %s", job_id, exc.detail)
    except Exception as exc:
        with session_scope() as db:
            JobsRepository(db).fail(
                job_id,
                str(exc),
                retryable=True,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
        logger.exception("estimate job %s failed for %s", job_id, record_id)
    return True


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    report = run_preflight(settings, component="worker")
    exit_on_failure(report)
    if settings.app_env not in ("local", "test"):
        from storage.db.migrate import assert_schema_at_head

        assert_schema_at_head()
        register_compute_node(settings)
    owner = process_lease_owner(settings.compute_node_id)
    stopped = False

    def request_stop(signum: int, frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Estimate worker started as %s", owner)
    idle_delay = 2.0
    while not stopped:
        if process_next_estimate(owner, settings.compute_node_id):
            idle_delay = 2.0
        else:
            # Every operator PC polls the same low-power NAS. Backing off while
            # idle keeps the UI responsive without a query every two seconds
            # from every workstation.
            time.sleep(idle_delay)
            idle_delay = min(30.0, idle_delay * 1.5)
    logger.info("Estimate worker stopped")


if __name__ == "__main__":
    main()
