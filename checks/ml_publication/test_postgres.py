"""Explicitly opted-in native SQL checks, separate from tests/conftest.py."""
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import OperationalError

if os.environ.get("PC_MODEL_PUBLICATION_PG_TEST") != "1":
    pytest.skip("Requires an isolated PostgreSQL cluster on port 55473", allow_module_level=True)

from analytics.prediction import retraining
from core.config.settings import get_settings
from core.versioning.constants import ANALYSIS_VERSION, APP_VERSION
from domain.models.jobs import BackgroundJob
from domain.models.ml import MLModelVersion
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession
from storage.db.base import Base
from storage.db.init_db import create_all
from storage.db.session import engine, session_scope
from storage.repositories import jobs_repo
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.model_registry import ModelRegistryRepository


@pytest.fixture(autouse=True)
def isolated_cluster():
    assert get_settings().app_env == "test"
    assert engine.url.host == "127.0.0.1" and engine.url.port == 55473
    assert engine.url.database == "pc_model_publication_test"
    create_all()
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())


def _seed():
    node = "pg-publication-probe"
    with session_scope() as db:
        db.add(BuildSession(session_id="pg-session", origin_compute_node_id=node,
            classification="REAL_PRINT", start_ts=datetime.now(timezone.utc),
            context={"runtime_payload": {"group": {"features": {"layers": 100},
                "analysis_snapshot": {"schema_version": 1, "analysis_id": "pg-probe",
                    "features": {"layers": 100}, "health": {}, "data_quality": {}, "signal_stats": {},
                    "provenance": {"analysis_version": ANALYSIS_VERSION}}}}}))
        db.flush()
        db.add(QualityOutcome(outcome_id="pg-qc", session_id="pg-session", result="accepted",
            is_final=True, timestamp=datetime.now(timezone.utc), inspection_type="visual"))
        db.flush()
        job = JobsRepository(db).enqueue(job_type=retraining.JOB_TYPE, owner_node_id=node,
            entity_type="ml_model", entity_id=retraining.MODEL_NAME, idempotency_key="pg-probe",
            payload={"owner_node_id": node})
    with session_scope() as db:
        job = JobsRepository(db).claim_next(retraining.JOB_TYPE, owner_node_id=node,
            lease_owner="pg-worker", lease_seconds=120)
    with session_scope() as db:
        prepared = retraining.prepare_retraining(db)
    fingerprint = retraining.training_fingerprint(prepared["rows"])
    # Explicit artifact double tests publication, not statistical or physical accuracy.
    candidate = {"model_name": retraining.MODEL_NAME, "algorithm": "publication-test-double",
        "owner_node_id": node, "training_fingerprint": fingerprint, "feature_schema_hash": "test",
        "training_session_ids": ["pg-session"], "training_size": 1, "positive_count": 0,
        "artifact": {}, "metrics": {}, "quality_gates": {}, "app_version": APP_VERSION,
        "analysis_version": ANALYSIS_VERSION}
    result = {"model_name": retraining.MODEL_NAME, "label_count": 1,
        "training_fingerprint": fingerprint, "candidate": candidate}
    args = {"expected_inputs": retraining.retraining_input_identity(prepared),
        "job_id": job["job_id"], "lease_owner": "pg-worker",
        "lease_generation": job["lease_generation"]}
    return result, args


@pytest.mark.parametrize("source", ["sessions", "quality_outcomes", "new_qc"])
def test_nowait_rejects_writer_in_progress_without_partial_publication(source):
    result, args = _seed()
    with engine.connect() as writer:
        tx = writer.begin()
        try:
            if source == "new_qc":
                writer.execute(insert(QualityOutcome).values(outcome_id="new-qc", session_id="pg-session", result="accepted", is_final=True, timestamp=datetime.now(timezone.utc), inspection_type="visual"))
            elif source == "sessions":
                writer.execute(text("UPDATE sessions SET classification = 'REAL_PRINT' WHERE session_id = 'pg-session'"))
            else:
                writer.execute(text("UPDATE quality_outcomes SET result = 'rejected' WHERE outcome_id = 'pg-qc'"))
            started = time.monotonic()
            with pytest.raises(OperationalError) as caught:
                with session_scope() as db:
                    retraining.publish_retraining(db, result, **args)
            assert caught.value.orig.sqlstate == "55P03"
            assert time.monotonic() - started < 2
        finally:
            tx.rollback()
    with session_scope() as db:
        assert db.scalars(select(MLModelVersion)).all() == []
        assert db.get(BackgroundJob, args["job_id"]).status == "running"
        # Existing bounded retry budget returns the consumed attempt.
        JobsRepository(db).defer_retry(args["job_id"], "busy source", lease_owner="pg-worker",
            lease_generation=args["lease_generation"])
    with session_scope() as db:
        job = db.get(BackgroundJob, args["job_id"])
        assert job.status == "pending" and job.attempts == 0 and not job.result_json


def test_source_guard_blocks_new_rows_until_commit_but_allows_readers(monkeypatch):
    result, args = _seed()
    persist = retraining.persist_retraining

    def check_protection(db, calculated):
        with engine.connect() as reader:
            assert reader.scalar(text("SELECT count(*) FROM sessions")) == 1
        with engine.connect() as writer:
            writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(OperationalError) as caught:
                writer.execute(insert(QualityOutcome).values(outcome_id="phantom-qc", session_id="pg-session", result="accepted", is_final=True, timestamp=datetime.now(timezone.utc), inspection_type="visual"))
            assert caught.value.orig.sqlstate == "55P03"
        return persist(db, calculated)

    monkeypatch.setattr(retraining, "persist_retraining", check_protection)
    with session_scope() as db:
        retraining.publish_retraining(db, result, **args)
    with session_scope() as db:
        assert db.get(BackgroundJob, args["job_id"]).status == "done"
        assert ModelRegistryRepository(db).get_shadow(retraining.MODEL_NAME) is not None


def test_committed_qc_change_is_rejected():
    result, args = _seed()
    with session_scope() as db:
        db.get(QualityOutcome, "pg-qc").result = "rejected"
    with pytest.raises(retraining.RetrainingInputsChanged):
        with session_scope() as db:
            retraining.publish_retraining(db, result, **args)
    with session_scope() as db:
        assert ModelRegistryRepository(db).get_shadow(retraining.MODEL_NAME) is None


@pytest.mark.parametrize("kind", ["candidate", "promotion"])
def test_real_job_row_wait_rechecks_clock_and_rolls_back_registry(monkeypatch, kind):
    result, args = _seed()
    if kind == "promotion":
        with session_scope() as db:
            registry = ModelRegistryRepository(db)
            champion = dict(result["candidate"], training_fingerprint="earlier-test-fingerprint")
            active = registry.register_shadow(champion)
            registry.promote(active["model_version_id"], "earlier probe")
            shadow = registry.register_shadow(result["candidate"])
            args["expected_inputs"] = retraining.retraining_input_identity(retraining.prepare_retraining(db))
        result.update(candidate=None, shadow_model_version_id=shadow["model_version_id"],
            evaluation={"decision": "promote", "reason": "controlled publication probe", "metrics": {}})
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(jobs_repo, "_now", lambda: clock[0])
    persist = retraining.persist_retraining
    publication_waiting = threading.Event()
    failures = []

    def staged(db, calculated):
        published = persist(db, calculated)
        publication_waiting.set()
        return published

    monkeypatch.setattr(retraining, "persist_retraining", staged)

    def publish():
        try:
            with session_scope() as db:
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                retraining.publish_retraining(db, result, **args)
        except Exception as error:
            failures.append(error)

    with engine.connect() as blocker:
        tx = blocker.begin()
        blocker.execute(text("SELECT job_id FROM background_jobs WHERE job_id = :id FOR UPDATE"), {"id": args["job_id"]})
        thread = threading.Thread(target=publish)
        thread.start()
        try:
            assert publication_waiting.wait(3)
            # Actual SQL lock wait, deterministic lease clock advancement.
            time.sleep(0.1)
            assert thread.is_alive()
            clock[0] += timedelta(seconds=121)
        finally:
            tx.rollback()
            thread.join(6)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], retraining.StaleModelLease)
    with session_scope() as db:
        registry = ModelRegistryRepository(db)
        if kind == "candidate":
            assert db.scalars(select(MLModelVersion)).all() == []
        else:
            assert registry.get_active(retraining.MODEL_NAME)["model_version_id"] == active["model_version_id"]
            assert registry.get_shadow(retraining.MODEL_NAME)["model_version_id"] == shadow["model_version_id"]
            assert registry.get_shadow(retraining.MODEL_NAME)["evaluated_at"] is None
        assert db.get(BackgroundJob, args["job_id"]).status == "running"


def test_registry_decisions_wait_for_lifecycle_before_row_lock():
    result, _ = _seed()
    with session_scope() as db:
        candidate = ModelRegistryRepository(db).register_shadow(result["candidate"])
    entered = threading.Event()
    errors = []

    def promote():
        try:
            with session_scope() as db:
                db.execute(text("SET LOCAL statement_timeout = '5s'"))
                entered.set()
                ModelRegistryRepository(db).promote(candidate["model_version_id"], "test")
        except Exception as error:
            errors.append(error)

    with session_scope() as db:
        registry = ModelRegistryRepository(db)
        registry.lock_lifecycle(retraining.MODEL_NAME)
        thread = threading.Thread(target=promote)
        thread.start()
        try:
            assert entered.wait(2)
            time.sleep(0.1)
            assert thread.is_alive()
            # If promote held this row before the lifecycle lock, NOWAIT fails.
            row = db.scalar(select(MLModelVersion).where(
                MLModelVersion.model_version_id == candidate["model_version_id"]
            ).with_for_update(nowait=True))
            assert row.status == "shadow"
        finally:
            db.commit()
            thread.join(6)
    assert not thread.is_alive() and errors == []
    with session_scope() as db:
        assert ModelRegistryRepository(db).get_active(retraining.MODEL_NAME)["model_version_id"] == candidate["model_version_id"]
