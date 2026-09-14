"""Dedicated single-owner worker for durable, CPU-heavy plate estimates."""

import logging
import signal
from threading import Event

from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from core.compute_identity import process_lease_owner, register_compute_node
from core.config.settings import get_settings
from core.logging.config import configure_logging
from core.preflight import exit_on_failure, run_preflight
from domain.services.estimation.contracts import EstimateError
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


def _defer_infrastructure_failure(
    job_id: str,
    error: Exception,
    *,
    lease_owner: str,
    lease_generation: int,
) -> None:
    """Best-effort durable release; an unavailable DB leaves lease recovery."""
    try:
        with session_scope() as db:
            JobsRepository(db).defer_infrastructure(
                job_id,
                str(error),
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
    except Exception:
        # If PostgreSQL is still offline there is nowhere else authoritative to
        # publish this state. Leave the fenced row running; its lease makes it
        # reclaimable by this same operator PC when the database returns.
        logger.exception("could not defer infrastructure failure for %s", job_id)


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
            raise EstimateError(
                "stale_inputs",
                "estimate job owner does not match worker/payload owner",
            )
        # Lazy import keeps the worker's startup/health path cheap.
        from domain.services.estimation.calculation import (
            calculate_prediction_snapshot, enrich_prediction_interval, needs_prediction_interval,
        )
        from domain.services.estimation.inputs import prepare_prediction_inputs
        from domain.services.estimation.publication import publish_estimate
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
                record = PrintsRepository(db).get_print_record(record_id)
                requested_revision = job['payload'].get('record_revision')
                if record is None or requested_revision != record['revision']:
                    raise EstimateError("stale_inputs", 'Карточка изменилась после постановки расчёта в очередь. Запустите новый расчёт.')
                prepared = prepare_prediction_inputs(
                    PrintsRepository(db),
                    record_id,
                    compute_node_id=job["owner_node_id"],
                )

            # MinIO download, mesh slicing, physics and ML all run locally with
            # no PostgreSQL connection checked out from the NAS.
            snapshot = calculate_prediction_snapshot(
                prepared,
                geometry_cache=_DetachedGeometryCache(),
                computed_by=owner_node_id,
            )
            if needs_prediction_interval(snapshot):
                from analytics.prediction.calibration_inputs import load_calibration_inputs

                with session_scope() as db:
                    history = load_calibration_inputs(db)
                enrich_prediction_interval(snapshot, inputs=history)
        if heartbeat.lost:
            logger.warning("discarded calculation after losing lease for %s", job_id)
            return True

        # Publication owns the input and lease checks around the card write.
        # Late expiry raises and rolls back instead of returning from inside
        # a context manager that would commit a partial change.
        with session_scope() as db:
            publish_estimate(
                db, prepared, snapshot, job_id=job_id, owner_node_id=owner_node_id,
                lease_owner=lease_owner, lease_generation=lease_generation,
            )
        logger.info("estimate job %s completed for %s", job_id, record_id)
    except EstimateError as exc:
        if exc.code == "lease_lost":
            logger.warning("discarded estimate %s: %s", job_id, exc.detail)
            return True
        if exc.code == "storage_unavailable":
            _defer_infrastructure_failure(
                job_id,
                exc,
                lease_owner=lease_owner,
                lease_generation=lease_generation,
            )
        else:
            with session_scope() as db:
                JobsRepository(db).fail(
                    job_id,
                    str(exc.detail),
                    retryable=False,
                    lease_owner=lease_owner,
                    lease_generation=lease_generation,
                )
        logger.warning("estimate job %s rejected: %s", job_id, exc.detail)
    except (OperationalError, SQLAlchemyTimeoutError) as exc:
        _defer_infrastructure_failure(
            job_id,
            exc,
            lease_owner=lease_owner,
            lease_generation=lease_generation,
        )
        logger.warning("estimate job %s postponed after NAS database outage", job_id)
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


def run_worker_loop(lease_owner: str, owner_node_id: str, stopped: Event) -> None:
    """Keep this operator's queues alive across claim/finalization outages.

    A database failure can happen before a job is claimed, or while recording
    its failure. Neither belongs to a model's retry budget. The durable lease
    remains the recovery/fencing authority; this loop only waits and retries.
    """
    from worker.model_tasks import process_next_model_task
    from worker.calibration_tasks import process_next_calibration_task

    delay = 2.0
    while not stopped.is_set():
        try:
            estimated = process_next_estimate(lease_owner, owner_node_id)
            # Do not short-circuit on a busy estimate queue: all three job types
            # belong to this PC and must eventually get a turn.
            trained = False if stopped.is_set() else process_next_model_task(lease_owner, owner_node_id)
            calibrated = False if stopped.is_set() else process_next_calibration_task(lease_owner, owner_node_id)
        except (OperationalError, SQLAlchemyTimeoutError):
            logger.warning("Estimate/model/calibration queues unavailable; retrying in %.1fs", delay)
        except Exception:
            logger.exception("Estimate/model/calibration worker loop failed; retrying in %.1fs", delay)
        else:
            if estimated or trained or calibrated:
                delay = 2.0
                continue
        # Event.wait lets SIGTERM/SIGINT interrupt an idle/outage backoff.
        stopped.wait(delay)
        delay = min(30.0, delay * 1.5)


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
    stopped = Event()

    def request_stop(signum: int, frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logger.info("Estimate worker started as %s", owner)
    run_worker_loop(owner, settings.compute_node_id, stopped)
    logger.info("Estimate worker stopped")


if __name__ == "__main__":
    main()
