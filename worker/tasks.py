from pathlib import Path
from datetime import datetime, timezone
import logging
import signal
import time

from core.config.settings import get_settings
from core.logging.config import configure_logging
from core.preflight import run_preflight, exit_on_failure
from domain.enums.common import ImportJobStatus
from domain.services.import_jobs import retry_import_job
from domain.services.ingestion import IngestionService
from profiles.m350.profile import build_registry, get_profile
from reporting.json_report.generator import generate_session_json_report
from storage.db.init_db import create_all
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


logger = logging.getLogger(__name__)


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
    return generate_session_json_report(session_id, result.files)


def process_due_import_jobs() -> int:
    now = datetime.now(timezone.utc)
    processed = 0
    failed = 0
    registry = build_registry()
    profile = get_profile()
    settings = get_settings()

    # Read pending job IDs first in a short-lived session.
    # Each job then gets its OWN session so a DB error in one job
    # (e.g. PostgreSQL 1 GB jsonb limit) cannot roll back all others.
    pending_ids: list[str] = []
    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        for job in repo.list_import_jobs():
            due_postponed = (
                job.status == ImportJobStatus.postponed
                and job.confirmed_by is not None
                and job.postponed_until is not None
                and job.postponed_until <= now
            )
            if job.status == ImportJobStatus.checking_stability or due_postponed:
                pending_ids.append(job.import_job_id)

    for job_id in pending_ids:
        # Fresh session per job — a failed commit here cannot poison other jobs.
        with SessionLocal() as db:
            repo = RuntimeRepository(db)
            job = repo.get_import_job(job_id)
            if job is None:
                continue

            logger.info("Processing import job %s from %s", job.import_job_id, job.source_path)
            try:
                result = retry_import_job(
                    job,
                    registry=registry,
                    profile=profile,
                    actor="worker",
                    settings=settings,
                    now=now,
                )
                repo.save_import_job(result.job)
                repo.save_notifications(result.notifications)
                repo.save_sessions(result.sessions)
                repo.save_reports(result.reports)
                from domain.services.print_linking import auto_link_print_records

                auto_link_print_records(db)
                db.commit()
                processed += 1
                logger.info("Successfully processed import job %s", job.import_job_id)
            except Exception as exc:
                failed += 1
                logger.error(
                    "Failed to process import job %s: %s",
                    job.import_job_id,
                    exc,
                    exc_info=True,
                )
                # Save failed status in a brand-new session (the current one
                # is in a rolled-back state and cannot be used).
                try:
                    with SessionLocal() as db2:
                        repo2 = RuntimeRepository(db2)
                        job2 = repo2.get_import_job(job_id)
                        if job2:
                            job2.status = ImportJobStatus.failed
                            job2.updated_at = now
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
    for warn in report.warnings:
        logger.warning("PREFLIGHT: %s", warn)
    if settings.app_env == "local":
        create_all()
    
    stop = False
    backoff = ExponentialBackoff(base_delay=5.0, max_delay=60.0)

    def _request_stop(signum: int, frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    logger.info("Worker started. Waiting for ingestion and analysis jobs.")
    
    while not stop:
        try:
            processed = process_due_import_jobs()
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
