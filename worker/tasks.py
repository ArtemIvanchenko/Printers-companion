from pathlib import Path
from datetime import datetime, timedelta, timezone
import logging
import signal
import time

from core.compute_identity import process_lease_owner, register_compute_node
from core.config.settings import get_settings
from core.logging.config import configure_logging
from core.preflight import run_preflight, exit_on_failure
from domain.enums.common import ImportJobStatus
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from domain.services.compute_affinity import ComputeAffinityError, require_compute_owner
from domain.services.import_jobs import (
    ImportJobRecord,
    LeaseCheckUnavailableError,
    RetryableImportError,
    StaleImportLeaseError,
    retry_import_job,
)
from domain.services.ingestion import IngestionService
from profiles.m350.profile import build_registry, get_profile
from reporting.json_report.generator import generate_session_json_report
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository
from worker.lease_heartbeat import LeaseHeartbeat


logger = logging.getLogger(__name__)


def _require_import_entity_owners(
    db,
    *,
    owner_node_id: str,
    print_record_id: str | None = None,
    session_ids: list[str] | None = None,
) -> None:
    """Fence a claimed job against domain ownership, not only job ownership."""
    if print_record_id:
        record = db.get(PrintRecord, print_record_id)
        if record is None:
            raise RuntimeError(f"print record '{print_record_id}' no longer exists")
        require_compute_owner(
            entity_type="print_record",
            entity_id=record.record_id,
            origin_compute_node_id=record.origin_compute_node_id,
            requested_compute_node_id=owner_node_id,
        )
    for session_id in session_ids or []:
        session = db.get(BuildSession, session_id)
        if session is None:
            raise RuntimeError(f"session '{session_id}' was not persisted")
        require_compute_owner(
            entity_type="session",
            entity_id=session.session_id,
            origin_compute_node_id=session.origin_compute_node_id,
            requested_compute_node_id=owner_node_id,
        )


def _lease_expired(lease_until: datetime | None) -> bool:
    if lease_until is None:
        return True
    if lease_until.tzinfo is None:
        lease_until = lease_until.replace(tzinfo=timezone.utc)
    return lease_until <= datetime.now(timezone.utc)


def _apply_import_failure_policy(
    job: ImportJobRecord,
    error: Exception,
    *,
    settings,
    now: datetime,
) -> None:
    """Keep infrastructure outages retryable; bound only real worker bugs."""
    job.error = str(error)[:4000]
    if isinstance(error, ComputeAffinityError):
        # Ownership conflicts require an operator/admin to fix the entity, not
        # another automatic raw parse.
        job.status = ImportJobStatus.failed
        job.postponed_until = None
    elif isinstance(error, RetryableImportError):
        # NAS/database availability is not bad input and must not consume the
        # bounded parser/stability budget. lease_generation is monotonic,
        # providing backoff without a new schema column.
        retry = min(
            settings.nas_sync_retry_max_seconds,
            settings.nas_sync_retry_min_seconds
            * (2 ** min(max(0, job.lease_generation - 1), 16)),
        )
        job.status = ImportJobStatus.postponed
        job.postponed_until = now + timedelta(seconds=retry)
    else:
        # Unexpected worker bugs retain a bounded retry budget so malformed
        # input cannot spin forever.
        job.stability_check_attempts += 1
        if job.stability_check_attempts < settings.file_stability_max_retries:
            job.status = ImportJobStatus.postponed
            job.postponed_until = now + timedelta(
                seconds=settings.file_stability_retry_seconds
            )
        else:
            job.status = ImportJobStatus.failed
            job.postponed_until = None
    job.updated_at = now
    job.lease_owner = None
    job.lease_until = None


class ExponentialBackoff:
    """Exponential backoff with jitter for retries."""
    
    def __init__(self, base_delay: float = 1.0, max_delay: float = 60.0, jitter: bool = True):
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.current_delay = base_delay
    
    def get_delay(self) -> float:
        """Get next delay with optional jitter."""
        import random
        delay = self.current_delay
        if self.jitter:
            delay = delay * (0.5 + random.random())  # ±50% jitter
        return min(delay, self.max_delay)
    
    def next(self) -> None:
        """Calculate next delay (exponential growth)."""
        self.current_delay = min(self.current_delay * 2, self.max_delay)
    
    def reset(self) -> None:
        """Reset to initial delay."""
        self.current_delay = self.base_delay


def ingest_folder(folder: str) -> dict:
    result = IngestionService(build_registry(), get_profile()).parse(Path(folder))
    return result.model_dump(mode="json")


def analyze_folder(folder: str, session_id: str = "local_session") -> dict:
    result = IngestionService(build_registry(), get_profile()).parse(Path(folder))
    from analytics.log_insights.pipeline import build_log_insights

    report = generate_session_json_report(session_id, result.files)
    report["log_insights"] = build_log_insights(
        result.files, [e for f in result.files if f.parse_result for e in f.parse_result.events],
    )
    return report


def process_due_import_jobs(lease_owner: str | None = None) -> int:
    processed = 0
    failed = 0
    registry = build_registry()
    profile = get_profile()
    settings = get_settings()
    lease_owner = lease_owner or process_lease_owner(settings.compute_node_id)

    while True:
        # Claim under a row lock, then commit the short transaction before the
        # long local parse.  The WHERE clause includes owner_node_id, so a PC
        # never downloads or executes another operator PC's work.
        now = datetime.now(timezone.utc)
        with SessionLocal() as claim_db:
            claimed = RuntimeRepository(claim_db).claim_next_import_job(
                owner_node_id=settings.compute_node_id,
                lease_owner=lease_owner,
                now=now,
                lease_seconds=settings.job_lease_seconds,
            )
            claim_db.commit()
        if claimed is None:
            break
        job_id = claimed.import_job_id
        lease_generation = claimed.lease_generation

        logger.info(
            "Processing import job %s from %s on node %s",
            claimed.import_job_id,
            claimed.source_path,
            settings.compute_node_id,
        )
        try:
            # Recheck the referenced card before touching local files or MinIO.
            # Job affinity alone is insufficient if a malformed/stale job row
            # points at a card created by another operator PC.
            with SessionLocal() as affinity_db:
                _require_import_entity_owners(
                    affinity_db,
                    owner_node_id=claimed.owner_node_id,
                    print_record_id=claimed.print_record_id,
                )

            def renew_lease() -> bool:
                with SessionLocal() as heartbeat_db:
                    renewed = RuntimeRepository(heartbeat_db).renew_import_job_lease(
                        job_id,
                        lease_owner=lease_owner,
                        lease_generation=lease_generation,
                        lease_seconds=settings.job_lease_seconds,
                    )
                    heartbeat_db.commit()
                    return renewed

            # All file IO, parsing and analytics happen with no remote database
            # transaction held open. A tiny configurable heartbeat keeps very
            # large log batches from looking abandoned.
            with LeaseHeartbeat(
                renew_lease,
                description=f"import:{job_id}",
                interval_seconds=settings.job_heartbeat_seconds,
            ) as heartbeat:
                last_guard_check = 0.0

                def current_lease() -> bool:
                    nonlocal last_guard_check
                    if heartbeat.lost:
                        return False
                    checked_at = time.monotonic()
                    # Persistence checkpoints can occur every 500 events. One
                    # fenced DB check per 30 seconds is enough; checking every
                    # batch would add thousands of writes to a weak NAS.
                    if checked_at - last_guard_check < 30.0:
                        return True
                    try:
                        current = renew_lease()
                    except Exception as exc:
                        raise LeaseCheckUnavailableError(
                            f"Could not verify import lease: {exc}"
                        ) from exc
                    last_guard_check = checked_at
                    return current

                result = retry_import_job(
                    claimed,
                    registry=registry,
                    profile=profile,
                    actor="worker",
                    settings=settings,
                    now=now,
                    lease_guard=current_lease,
                )
            if heartbeat.lost:
                logger.warning("Discarding import result after lost lease: %s", job_id)
                continue
            result.job.lease_owner = None
            result.job.lease_until = None

            with SessionLocal() as db:
                repo = RuntimeRepository(db)
                # Serialize finalization with lease reclaim and operator
                # Retry/Ignore/Postpone. The fence must be checked while the
                # row lock is held; a plain read followed by an upsert lets a
                # stale process overwrite a newly claimed generation.
                current = repo.get_import_job_for_update(job_id)
                # Fencing check: ignore a late result if an operator retried,
                # ignored or otherwise replaced this exact lease meanwhile.
                if (
                    current is None
                    or current.owner_node_id != settings.compute_node_id
                    or current.lease_owner != lease_owner
                    or current.lease_generation != lease_generation
                    or _lease_expired(current.lease_until)
                ):
                    logger.warning("Discarding stale result for import job %s", job_id)
                    continue
                # Browser upload can attach an explicit card while this batch
                # is already being parsed after the watcher discovered it.
                # Keep that stronger identity from the locked current row;
                # the claimed Pydantic snapshot predates the attachment.
                if current.print_record_id and not result.job.print_record_id:
                    result.job.print_record_id = current.print_record_id
                _require_import_entity_owners(
                    db,
                    owner_node_id=current.owner_node_id,
                    print_record_id=result.job.print_record_id,
                    session_ids=result.job.session_ids,
                )
                repo.save_import_job(result.job)
                repo.save_notifications(result.notifications)
                repo.save_sessions(
                    result.sessions,
                    origin_compute_node_id=current.owner_node_id,
                )
                repo.save_reports(result.reports)
                from domain.services.print_linking import auto_link_print_records

                links: list[dict] = []
                if result.job.print_record_id and len(result.job.session_ids) == 1:
                    from domain.models.sessions import BuildSession
                    from storage.repositories.prints_repo import PrintsRepository

                    session_id = result.job.session_ids[0]
                    session = db.get(BuildSession, session_id)
                    if PrintsRepository(db).link_session(
                        result.job.print_record_id,
                        session_id,
                        session.start_ts if session else None,
                        compute_node_id=current.owner_node_id,
                    ):
                        links.append({
                            "record_id": result.job.print_record_id,
                            "session_id": session_id,
                        })
                links.extend(
                    auto_link_print_records(
                        db,
                        origin_compute_node_id=current.owner_node_id,
                    )
                )
                if links:
                    # A strict quality verdict may have been entered on the
                    # card before logs arrived. Linking creates the first
                    # training-grade session/outcome pair, so enqueue its
                    # owner-local model refresh now.
                    from analytics.prediction.retraining import (
                        enqueue_retraining_for_session,
                    )

                    for linked_session_id in {
                        str(link["session_id"]) for link in links if link.get("session_id")
                    }:
                        enqueue_retraining_for_session(db, linked_session_id)
                    # New predicted/actual pairs appeared → refresh per-material
                    # time-correction factors automatically.
                    from analytics.prediction.accuracy import (
                        recalibrate_and_apply,
                        try_acquire_calibration_lock,
                    )
                    from analytics.prediction.recoat_calibration import (
                        recalibrate_recoat_and_apply,
                    )
                    from analytics.prediction.scan_calibration import (
                        recalibrate_scan_and_apply,
                    )
                    try:
                        if try_acquire_calibration_lock(db):
                            recalibrate_and_apply(db)
                            recalibrate_recoat_and_apply(db)
                            recalibrate_scan_and_apply(db)
                        else:
                            logger.info(
                                "Auto-calibration already runs on another operator PC; skipped"
                            )
                    except Exception:
                        logger.exception("auto-calibration after linking failed")
                db.commit()
                processed += 1
                logger.info("Successfully processed import job %s", claimed.import_job_id)
        except Exception as exc:
            failed += 1
            logger.error(
                "Failed to process import job %s: %s",
                claimed.import_job_id,
                exc,
                exc_info=True,
            )
            # Save failed status in a brand-new session (the current one
            # is in a rolled-back state and cannot be used).
            try:
                with SessionLocal() as db2:
                    repo2 = RuntimeRepository(db2)
                    job2 = repo2.get_import_job_for_update(job_id)
                    if (
                        job2
                        and job2.owner_node_id == settings.compute_node_id
                        and job2.lease_owner == lease_owner
                        and job2.lease_generation == lease_generation
                        and not _lease_expired(job2.lease_until)
                    ):
                        failure_now = datetime.now(timezone.utc)
                        job2.error = str(exc)[:4000]
                        if isinstance(exc, StaleImportLeaseError):
                            # A valid fence normally cannot reach this branch;
                            # leave the row reclaimable rather than publishing
                            # a terminal state from a stale worker.
                            continue
                        _apply_import_failure_policy(
                            job2,
                            exc,
                            settings=settings,
                            now=failure_now,
                        )
                        repo2.save_import_job(job2)
                        db2.commit()
            except Exception as db_exc:
                logger.error(
                    "Failed to persist error state for job %s: %s",
                    job_id,
                    db_exc,
                )

    if failed > 0:
        logger.warning("Processed %s job(s) successfully, %s failed", processed, failed)

    return processed


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    report = run_preflight(settings, component="worker")
    exit_on_failure(report)
    if settings.app_env not in ("local", "test"):
        from storage.db.migrate import assert_schema_at_head

        assert_schema_at_head()
        register_compute_node(settings)
    for warn in report.warnings:
        logger.warning("PREFLIGHT: %s", warn)
    stop = False
    backoff = ExponentialBackoff(base_delay=5.0, max_delay=60.0)

    def _request_stop(signum: int, frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    lease_owner = process_lease_owner(settings.compute_node_id)
    logger.info(
        "Worker started on compute node %s. Waiting for its local ingestion jobs.",
        settings.compute_node_id,
    )
    
    while not stop:
        try:
            processed = process_due_import_jobs(lease_owner)
            if processed:
                logger.info("Processed %s import job(s)", processed)
                backoff.reset()  # Reset backoff on successful processing
            else:
                # No jobs processed; sleep with backoff
                delay = backoff.get_delay()
                logger.debug("No jobs ready. Waiting %.1fs before next check", delay)
                time.sleep(delay)
                backoff.next()  # Increase delay for next iteration
        except Exception as exc:
            # Log error with context and sleep with backoff
            logger.exception("Worker import-job loop failed: %s", exc)
            delay = backoff.get_delay()
            logger.info("Retrying in %.1fs", delay)
            time.sleep(delay)
            backoff.next()
    
    logger.info("Worker stopped.")


if __name__ == "__main__":
    main()
