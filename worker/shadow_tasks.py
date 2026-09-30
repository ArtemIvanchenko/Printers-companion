"""Timeout-isolated experiments; failure cannot roll back a completed import."""
import logging
import multiprocessing

from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError

from core.config.settings import get_settings
from domain.services.shadow_analysis import JOB_TYPE, calculate_shadow, shadow_inputs
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker.lease_heartbeat import LeaseHeartbeat

logger = logging.getLogger(__name__)


def _child_calculate(channel, inputs, owner_node_id):
    try:
        channel.send((True, calculate_shadow(inputs, owner_node_id)))
    except Exception as exc:
        channel.send((False, f"{type(exc).__name__}: {exc}"))
    finally:
        channel.close()


def isolated_calculate(inputs, owner_node_id, timeout_seconds):
    """Spawn only our own bounded child; never fork a process with a SQL pool."""
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_child_calculate, args=(writer, inputs, owner_node_id), daemon=True)
    try:
        process.start()
        writer.close()
        if not reader.poll(timeout_seconds):
            raise TimeoutError("Превышен лимит времени экспериментального анализа")
        ok, value = reader.recv()
        if not ok:
            raise RuntimeError(value)
        return value
    finally:
        reader.close()
        writer.close()
        if process.pid is not None:
            process.join(timeout=1)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join(timeout=2)
            process.close()


def process_next_shadow_task(lease_owner, owner_node_id):
    settings = get_settings()
    with session_scope() as db:
        job = JobsRepository(db).claim_next(JOB_TYPE, owner_node_id=owner_node_id,
                                          lease_owner=lease_owner, lease_seconds=settings.job_lease_seconds)
    if job is None:
        return False
    job_id = job["job_id"]
    fence = {"lease_owner": lease_owner, "lease_generation": job["lease_generation"]}

    def renew():
        with session_scope() as db:
            return JobsRepository(db).renew_lease(job_id, **fence, lease_seconds=settings.job_lease_seconds)

    try:
        with session_scope() as db:
            inputs = shadow_inputs(db, job["entity_id"], owner_node_id)
        if (inputs["analysis_id"] != job["payload"].get("analysis_id")
                or inputs["input_fingerprint"] != job["payload"].get("input_fingerprint")):
            raise ValueError("Анализ сессии изменился: нужно новое экспериментальное задание")
        with LeaseHeartbeat(renew, description=f"shadow:{job_id}",
                            interval_seconds=settings.job_heartbeat_seconds) as heartbeat:
            result = isolated_calculate(inputs, owner_node_id, settings.shadow_analysis_timeout_seconds)
        if heartbeat.lost:
            return True
        with session_scope() as db:
            current = shadow_inputs(db, job["entity_id"], owner_node_id, lock=True)
            if (current["analysis_id"] != inputs["analysis_id"]
                    or current["input_fingerprint"] != inputs["input_fingerprint"]):
                raise ValueError("Результат устарел за время эксперимента")
            # Completion checks the fresh lease AFTER waiting for session lock.
            # Only the job result changes; no card, report or model is promoted.
            JobsRepository(db).complete(job_id, result, **fence)
    except (OperationalError, SQLAlchemyTimeoutError) as exc:
        try:
            with session_scope() as db:
                JobsRepository(db).defer_infrastructure(job_id, str(exc), **fence)
        except Exception:
            logger.exception("could not defer shadow task after NAS outage")
    except Exception as exc:
        with session_scope() as db:
            JobsRepository(db).fail(job_id, str(exc), retryable=False, **fence)
        logger.warning("Optional shadow experiment failed: %s", exc)
    return True
