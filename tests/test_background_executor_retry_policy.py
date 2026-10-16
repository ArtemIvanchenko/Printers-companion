"""Actual worker failure boundaries on isolated SQL, without ML or NAS IO."""

from datetime import datetime, timedelta, timezone

import pytest
from psycopg.errors import ConnectionFailure, InvalidPassword, TooManyConnections
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from analytics.prediction import retraining
from analytics.prediction.calibration import enqueue_calibration
from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from domain.models.ml import MLModelVersion
from domain.models.prints import MachineParams
from domain.models.sessions import BuildSession
from domain.services.shadow_analysis import request_shadow_analysis
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker import calibration_tasks, model_tasks, shadow_tasks


MODULES = {"model": model_tasks, "calibration": calibration_tasks, "shadow": shadow_tasks}
PROCESSORS = {
    "model": model_tasks.process_next_model_task,
    "calibration": calibration_tasks.process_next_calibration_task,
    "shadow": shadow_tasks.process_next_shadow_task,
}


@pytest.fixture
def executor_job(kind):
    node = get_settings().compute_node_id
    group = {"classification": "REAL_PRINT", "telemetry": {"time": []},
             "analysis_snapshot": {"schema_version": 1, "analysis_id": "executor-analysis",
                                   "telemetry_evidence": {"sample_count": 0}}}
    with session_scope() as db:
        db.add(MachineParams(id=1, correction_locked=True, time_correction_by_mat={"keep": 1.25}))
        db.add(BuildSession(session_id="executor-session", origin_compute_node_id=node,
                            context={"runtime_payload": {"group": group}}))
    with session_scope() as db:
        if kind == "model":
            job = JobsRepository(db).enqueue(
                job_type=model_tasks.JOB_TYPE, owner_node_id=node,
                entity_type="model", entity_id="defect-risk", idempotency_key="executor-model",
                payload={"owner_node_id": node},
            )
        elif kind == "calibration":
            job = enqueue_calibration(db, owner_node_id=node, trigger="test", event_id="executor-calibration")
        else:
            job = request_shadow_analysis(db, "executor-session", node)
    return job, group


def _sql_fault(db, failure):
    if failure == "missing_column":
        db.execute(text("SELECT missing_executor_column FROM background_jobs"))
    elif failure == "missing_function":
        db.execute(text("SELECT missing_executor_function(1)"))
    elif failure == "authentication":
        raise OperationalError(None, None, InvalidPassword("invalid configuration"))
    elif failure == "capacity":
        raise OperationalError(None, None, TooManyConnections("connection slots exhausted"))
    elif failure == "connection":
        raise OperationalError(None, None, ConnectionFailure("connection failed"))
    else:
        raise OperationalError("SQL", None, RuntimeError("disconnect"), connection_invalidated=True)


def _install_fault(monkeypatch, kind, phase, failure, job_id, before_error=None):
    def fail(db):
        if before_error:
            before_error()
        _sql_fault(db, failure)
    if kind == "model":
        prepare = model_tasks.prepare_retraining
        publish = retraining.persist_retraining
        def read(db):
            if phase == "read":
                fail(db)
            return prepare(db)
        def write(db, calculated):
            result = publish(db, calculated)
            assert db.get(BackgroundJob, job_id).status == "running"
            fail(db)
            return result
        monkeypatch.setattr(model_tasks, "prepare_retraining", read)
        if phase == "publication":
            monkeypatch.setattr(retraining, "persist_retraining", write)
    elif kind == "calibration":
        prepare = calibration_tasks.load_calibration_inputs
        publish = calibration_tasks.publish_calibration
        def read(db):
            if phase == "read":
                fail(db)
            return prepare(db)
        def write(db, calculated, **kwargs):
            result = publish(db, calculated, **kwargs)
            assert db.get(BackgroundJob, job_id).status == "done"
            fail(db)
            return result
        monkeypatch.setattr(calibration_tasks, "load_calibration_inputs", read)
        if phase == "publication":
            monkeypatch.setattr(calibration_tasks, "publish_calibration", write)
    else:
        prepare = shadow_tasks.shadow_inputs
        def read(db, session_id, node, **kwargs):
            result = prepare(db, session_id, node, **kwargs)
            if phase == "read" or kwargs.get("lock"):
                fail(db)
            return result
        monkeypatch.setattr(shadow_tasks, "shadow_inputs", read)
        monkeypatch.setattr(shadow_tasks, "isolated_calculate", lambda *a: {"mode": "shadow"})


def _assert_unpublished(db, group):
    assert db.get(MachineParams, 1).time_correction_by_mat == {"keep": 1.25}
    assert db.get(BuildSession, "executor-session").context == {"runtime_payload": {"group": group}}
    assert db.scalar(select(MLModelVersion)) is None


@pytest.mark.parametrize("kind", ["model", "calibration", "shadow"])
@pytest.mark.parametrize("phase", ["read", "publication"])
@pytest.mark.parametrize("failure,outage", [
    ("missing_column", False), ("missing_function", False), ("authentication", False),
    ("connection", True), ("capacity", True), ("invalidated", True),
])
def test_worker_sql_failures_have_bounded_bugs_and_durable_outages(
    executor_job, monkeypatch, kind, phase, failure, outage,
):
    job, group = executor_job
    node = get_settings().compute_node_id
    processor = PROCESSORS[kind]
    assert processor("foreign-worker", "foreign-node") is False
    _install_fault(monkeypatch, kind, phase, failure, job["job_id"])
    limit = job["max_attempts"] if kind != "shadow" else 1
    for attempt in range(1, (4 if outage else limit) + 1):
        assert processor("executor-worker", node)
        with session_scope() as db:
            row = db.get(BackgroundJob, job["job_id"])
            assert row.attempts == (0 if outage else attempt)
            assert row.lease_generation == attempt
            assert row.status == ("pending" if outage or attempt < limit else "failed")
            assert row.owner_node_id == node and row.lease_owner is None
            assert row.result_json == {}
            _assert_unpublished(db, group)
            row.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    if not outage:
        assert processor("executor-worker", node) is False


@pytest.mark.parametrize("kind", ["model", "calibration", "shadow"])
def test_outage_handler_cannot_mutate_reclaimed_or_terminal_job(executor_job, monkeypatch, kind):
    job, group = executor_job
    node = get_settings().compute_node_id
    replacement = {}
    def reclaim():
        with session_scope() as db:
            db.get(BackgroundJob, job["job_id"]).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        with session_scope() as db:
            JobsRepository(db).claim_next(MODULES[kind].JOB_TYPE, owner_node_id=node, lease_owner="replacement-worker")
            replacement.update(JobsRepository(db).get(job["job_id"]))
    _install_fault(monkeypatch, kind, "read", "connection", job["job_id"], before_error=reclaim)
    assert PROCESSORS[kind]("executor-worker", node)
    with session_scope() as db:
        current = JobsRepository(db).get(job["job_id"])
        for field in ("status", "lease_owner", "lease_generation", "attempts", "error", "result"):
            assert current[field] == replacement[field]
        _assert_unpublished(db, group)
