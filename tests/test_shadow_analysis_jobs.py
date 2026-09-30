from copy import deepcopy
from datetime import datetime

import pytest

from core.config.settings import get_settings
from domain.models.sessions import BuildSession
from domain.services.shadow_analysis import request_shadow_analysis
from storage.db.session import session_scope
from storage.repositories.jobs_repo import JobsRepository
from worker.shadow_tasks import process_next_shadow_task, isolated_calculate


@pytest.fixture
def shadow_job(monkeypatch):
    monkeypatch.setattr(get_settings(), "compute_node_id", "shadow-pc")
    group = {"classification": "REAL_PRINT", "telemetry": {"time": [], "layer_burn_times": []},
             "analysis_snapshot": {"schema_version": 1, "analysis_id": "analysis-test",
                                   "telemetry_evidence": {"sample_count": 1000}}}
    with session_scope() as db:
        db.add(BuildSession(session_id="shadow-session", origin_compute_node_id="shadow-pc",
                            start_ts=datetime(2026, 9, 1), context={"runtime_payload": {"group": group}}))
    with session_scope() as db:
        job = request_shadow_analysis(db, "shadow-session", "shadow-pc")
    return job, group


def test_repeated_request_is_idempotent_and_other_pc_cannot_claim(shadow_job):
    job, _ = shadow_job
    with session_scope() as db:
        assert request_shadow_analysis(db, "shadow-session", "shadow-pc")["job_id"] == job["job_id"]
    assert process_next_shadow_task("other-worker", "other-pc") is False


def test_shadow_failure_does_not_modify_published_analysis(shadow_job, monkeypatch):
    job, group = shadow_job
    def fail(*args):
        raise TimeoutError("test budget")
    monkeypatch.setattr("worker.shadow_tasks.isolated_calculate", fail)
    assert process_next_shadow_task("shadow-worker", "shadow-pc")
    with session_scope() as db:
        assert db.get(BuildSession, "shadow-session").context["runtime_payload"]["group"] == group
        result = JobsRepository(db).get(job["job_id"])
        assert result["status"] == "failed"
        assert "test budget" in result["error"]


def test_shadow_publication_rejects_changed_inputs(shadow_job, monkeypatch):
    job, group = shadow_job
    def calculate(*args):
        with session_scope() as db:
            row = db.get(BuildSession, "shadow-session")
            context = deepcopy(row.context)
            context["runtime_payload"]["group"]["analysis_snapshot"]["analysis_id"] = "new-analysis"
            row.context = context
        return {"mode": "shadow"}
    monkeypatch.setattr("worker.shadow_tasks.isolated_calculate", calculate)
    assert process_next_shadow_task("shadow-worker", "shadow-pc")
    with session_scope() as db:
        result = JobsRepository(db).get(job["job_id"])
        assert result["status"] == "failed"
        assert not result["result"]


def test_spawned_empty_experiment_returns_honest_preview_scope():
    result = isolated_calculate({"session_id": "s", "analysis_id": "a", "telemetry": {},
                                 "input_fingerprint": "fp", "evidence": {"sample_count": 0}}, "pc", 20)
    assert result["operator_action_allowed"] is False
    assert result["input_scope"] == "published_display_preview_and_measured_layer_times"
    assert result["source_sample_count"] == 0


def test_timeout_terminates_only_the_child_created_for_this_job(monkeypatch):
    from unittest.mock import Mock
    reader, writer, process, context = Mock(), Mock(), Mock(), Mock()
    reader.poll.return_value = False
    process.pid = 123
    process.is_alive.side_effect = [True, False]
    context.Pipe.return_value = (reader, writer)
    context.Process.return_value = process
    monkeypatch.setattr("worker.shadow_tasks.multiprocessing.get_context", lambda mode: context)
    with pytest.raises(TimeoutError, match="лимит времени"):
        isolated_calculate({}, "pc", 5)
    reader.poll.assert_called_once_with(5)
    reader.recv.assert_not_called()
    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()
    process.close.assert_called_once_with()
