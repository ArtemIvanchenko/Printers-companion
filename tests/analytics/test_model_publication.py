"""Real SQL/QC/worker publication; numerical fitting is an explicit double."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from analytics.prediction import retraining
from core.config.settings import get_settings
from core.versioning.constants import ANALYSIS_VERSION
from domain.models.jobs import BackgroundJob
from domain.models.ml import MLModelVersion
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession
from domain.services.print_cards import quality
from storage.db.session import engine, session_scope
from storage.repositories import jobs_repo
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.model_registry import ModelRegistryRepository
from storage.repositories.prints_repo import PrintsRepository
from worker import model_tasks


def _group(layers):
    return {"features": {"layers": layers}, "analysis_snapshot": {
        "schema_version": 1, "analysis_id": "publication-test",
        "provenance": {"analysis_version": ANALYSIS_VERSION},
        "features": {"layers": layers}, "health": {},
        "data_quality": {}, "signal_stats": {},
    }}


def _artifact(data):
    return {"type": "logreg", "features": ["layers"], "mean": [100.0],
            "scale": [1.0], "coef": [0.0], "intercept": 0.0,
            "cv_auc": 0.9, "cv_folds": 3, "n_train": len(data),
            "n_defects": sum(label for _, label in data)}


@pytest.fixture
def publication(monkeypatch):
    node = "publication-pc"
    monkeypatch.setattr(get_settings(), "compute_node_id", node)
    monkeypatch.setattr(get_settings(), "job_heartbeat_seconds", 600)
    monkeypatch.setattr(retraining, "train_defect_model", _artifact)
    with session_scope() as db:
        for index in range(20):
            sid = f"publication-{index:02d}"
            db.add(BuildSession(
                session_id=sid, origin_compute_node_id=node, classification="REAL_PRINT",
                start_ts=datetime(2026, 5, index + 1, tzinfo=timezone.utc),
                context={"runtime_payload": {"group": _group(100 + index)}},
            ))
            db.flush()
            card = PrintsRepository(db).create_print_record({
                "name": sid, "origin_compute_node_id": node, "session_id": sid,
            })
            if index < 19:
                db.add(QualityOutcome(
                    outcome_id=f"seed-{sid}", print_record_id=card["record_id"], session_id=sid,
                    timestamp=datetime(2026, 5, 21, tzinfo=timezone.utc),
                    inspection_type="visual", result="rejected" if index % 2 else "accepted",
                    is_final=True, inspection_result="Synthetic confirmed fixture",
                ))
    with session_scope() as db:
        initial = quality.create_quality_outcome(
            PrintsRepository(db), card["record_id"],
            {"result": "accepted", "inspection_result": "Synthetic initial inspection"},
            actor=node,
        )
    return node, card, initial


def _correct(publication):
    node, card, initial = publication
    with session_scope() as db:
        return quality.create_quality_outcome(
            PrintsRepository(db), card["record_id"],
            {"result": "rejected", "inspection_result": "Synthetic corrected inspection",
             "defect_type": "porosity", "defect_location": "body",
             "supersedes_outcome_id": initial["outcome_id"]}, actor=node,
        )


@pytest.mark.parametrize("change", ["none", "qc", "feature", "unlink", "new_label"])
def test_current_inputs_required_for_publication(monkeypatch, publication, change):
    node, card, initial = publication

    def during_cpu(data):
        if change == "qc":
            _correct(publication)
        elif change != "none":
            with session_scope() as db:
                if change == "feature":
                    row = db.get(BuildSession, card["session_id"])
                    row.context = {"runtime_payload": {"group": _group(999)}}
                elif change == "unlink":
                    db.get(QualityOutcome, initial["outcome_id"]).session_id = None
                else:
                    sid = "publication-new"
                    db.add(BuildSession(
                        session_id=sid, origin_compute_node_id=node, classification="REAL_PRINT",
                        start_ts=datetime(2026, 6, 1, tzinfo=timezone.utc),
                        context={"runtime_payload": {"group": _group(150)}},
                    ))
                    db.flush()
                    db.add(QualityOutcome(
                        outcome_id="new-outcome", session_id=sid, result="accepted", is_final=True,
                        timestamp=datetime(2026, 6, 2, tzinfo=timezone.utc), inspection_type="visual",
                    ))
        return _artifact(data)

    monkeypatch.setattr(retraining, "train_defect_model", during_cpu)
    assert model_tasks.process_next_model_task("publication-worker", owner_node_id=node)
    with session_scope() as db:
        job = db.get(BackgroundJob, initial["model_retraining_job_id"])
        shadow = ModelRegistryRepository(db).get_shadow(retraining.MODEL_NAME)
        if change == "none":
            assert job.status == "done" and job.attempts == 1
            assert shadow["training_fingerprint"] == retraining.training_fingerprint(retraining.prepare_retraining(db)["rows"])
            assert job.result_json["candidate_model_version_id"] == shadow["model_version_id"]
        else:
            assert job.status == "pending" and job.attempts == 0
            assert not job.result_json and job.available_at > job.created_at
            assert shadow is None


def test_fresh_other_owner_result_cannot_be_bound_to_stale_job(monkeypatch, publication):
    node, _, initial = publication
    other_node = "publication-other-pc"
    inside_other = False
    other_job = None

    def during_cpu(data):
        nonlocal inside_other, other_job
        if not inside_other:
            inside_other = True
            _correct(publication)
            with session_scope() as db:
                other_job = JobsRepository(db).enqueue(
                    job_type=retraining.JOB_TYPE, owner_node_id=other_node, entity_type="ml_model",
                    entity_id=retraining.MODEL_NAME, idempotency_key="other-owner",
                    payload={"owner_node_id": other_node},
                )
            assert model_tasks.process_next_model_task("other-worker", owner_node_id=other_node)
        return _artifact(data)

    monkeypatch.setattr(retraining, "train_defect_model", during_cpu)
    assert model_tasks.process_next_model_task("publication-worker", owner_node_id=node)
    with session_scope() as db:
        shadow = ModelRegistryRepository(db).get_shadow(retraining.MODEL_NAME)
        old = db.get(BackgroundJob, initial["model_retraining_job_id"])
        fresh = db.get(BackgroundJob, other_job["job_id"])
        assert old.status == "pending" and not old.result_json and old.attempts == 0
        assert fresh.status == "done" and shadow["owner_node_id"] == other_node
        assert shadow["positive_count"] == 10
        assert fresh.result_json["training_fingerprint"] == shadow["training_fingerprint"]
        assert fresh.result_json["candidate_model_version_id"] == shadow["model_version_id"]


def test_outdated_active_identity_is_still_fenced(monkeypatch, publication):
    node, _, initial = publication
    calculate = model_tasks.calculate_retraining

    def during_cpu(prepared, **kwargs):
        result = calculate(prepared, **kwargs)
        with session_scope() as db:
            candidate = deepcopy(result["candidate"])
            candidate["analysis_version"] = "old-feature-schema"
            registry = ModelRegistryRepository(db)
            old = registry.register_shadow(candidate)
            registry.promote(old["model_version_id"], "Synthetic legacy champion")
        return result

    monkeypatch.setattr(model_tasks, "calculate_retraining", during_cpu)
    assert model_tasks.process_next_model_task("publication-worker", owner_node_id=node)
    with session_scope() as db:
        registry = ModelRegistryRepository(db)
        assert registry.get_shadow(retraining.MODEL_NAME) is None
        assert registry.get_active(retraining.MODEL_NAME)["analysis_version"] == "old-feature-schema"
        assert db.get(BackgroundJob, initial["model_retraining_job_id"]).status == "pending"


@pytest.mark.parametrize("wait", [119, 121])
def test_lease_is_checked_after_registry_write_and_everything_rolls_back(monkeypatch, publication, wait):
    node, _, initial = publication
    clock = [datetime.now(timezone.utc)]
    monkeypatch.setattr(jobs_repo, "_now", lambda: clock[0])
    monkeypatch.setattr(get_settings(), "job_lease_seconds", 120)
    persist = retraining.persist_retraining

    def delayed_write(db, result):
        published = persist(db, result)
        assert db.get(BackgroundJob, initial["model_retraining_job_id"]).status == "running"
        clock[0] += timedelta(seconds=wait)
        return published

    monkeypatch.setattr(retraining, "persist_retraining", delayed_write)
    assert model_tasks.process_next_model_task("publication-worker", owner_node_id=node)
    with session_scope() as db:
        job = db.get(BackgroundJob, initial["model_retraining_job_id"])
        models = db.scalars(select(MLModelVersion)).all()
        if wait < 120:
            assert job.status == "done" and len(models) == 1
            assert job.finished_at.replace(tzinfo=timezone.utc) == clock[0]
        else:
            assert job.status == "running" and not job.result_json and models == []
            assert job.lease_until.replace(tzinfo=timezone.utc) < clock[0]


def test_changed_generation_cannot_publish(monkeypatch, publication):
    node, _, initial = publication

    def during_cpu(data):
        with session_scope() as db:
            job = db.get(BackgroundJob, initial["model_retraining_job_id"])
            job.lease_generation += 1
            job.lease_owner = "reclaimed-worker"
        return _artifact(data)

    monkeypatch.setattr(retraining, "train_defect_model", during_cpu)
    assert model_tasks.process_next_model_task("publication-worker", owner_node_id=node)
    with session_scope() as db:
        assert ModelRegistryRepository(db).get_shadow(retraining.MODEL_NAME) is None
        job = db.get(BackgroundJob, initial["model_retraining_job_id"])
        assert job.status == "running" and job.lease_owner == "reclaimed-worker"
        assert not job.result_json


def test_deferred_task_recalculates_fresh_inputs(monkeypatch, publication):
    node, card, initial = publication

    def change_features(data):
        with session_scope() as db:
            db.get(BuildSession, card["session_id"]).context = {
                "runtime_payload": {"group": _group(500)},
            }
        return _artifact(data)

    monkeypatch.setattr(retraining, "train_defect_model", change_features)
    assert model_tasks.process_next_model_task("publication-worker", owner_node_id=node)
    with session_scope() as db:
        deferred = db.get(BackgroundJob, initial["model_retraining_job_id"])
        assert deferred.status == "pending" and deferred.attempts == 0
        next_time = deferred.available_at.replace(tzinfo=timezone.utc) + timedelta(seconds=1)
    monkeypatch.setattr(jobs_repo, "_now", lambda: next_time)
    monkeypatch.setattr(retraining, "train_defect_model", _artifact)
    assert model_tasks.process_next_model_task("publication-next-worker", owner_node_id=node)
    with session_scope() as db:
        job = db.get(BackgroundJob, initial["model_retraining_job_id"])
        shadow = ModelRegistryRepository(db).get_shadow(retraining.MODEL_NAME)
        assert job.status == "done" and job.attempts == 1 and job.lease_generation == 2
        assert shadow["training_fingerprint"] == retraining.training_fingerprint(retraining.prepare_retraining(db)["rows"])


def test_sqlite_writer_contention_defers_without_spending_error_budget(monkeypatch, publication):
    node, _, initial = publication
    lock = ModelRegistryRepository.lock_lifecycle

    def busy_lifecycle(registry, name):
        # Actual independent SQLite writer, released before queue retry writes.
        with engine.connect() as writer:
            tx = writer.begin()
            try:
                writer.execute(text("UPDATE quality_outcomes SET result = result"))
                registry.db.connection().exec_driver_sql("PRAGMA busy_timeout=1")
                lock(registry, name)
            finally:
                tx.rollback()

    monkeypatch.setattr(ModelRegistryRepository, "lock_lifecycle", busy_lifecycle)
    assert model_tasks.process_next_model_task("publication-worker", owner_node_id=node)
    with session_scope() as db:
        job = db.get(BackgroundJob, initial["model_retraining_job_id"])
        assert job.status == "pending" and job.attempts == 0 and not job.result_json
        assert ModelRegistryRepository(db).get_shadow(retraining.MODEL_NAME) is None
