"""Registration is one SQL publication after detached candidate preparation."""

from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.config.settings import get_settings
from core.config.settings import Settings
from domain.enums.common import ImportJobStatus
from domain.services.import_jobs import detect_import_candidate
from domain.services.importing import requests
from storage.db.session import SessionLocal
from storage.db.session import session_scope
from storage.repositories.import_jobs import ImportJobsRepository
from storage.repositories.runtime import RuntimeRepository
from storage.repositories.reports import ReportsRepository


def _settings():
    return Settings(app_env="test", compute_node_id="request-owner", require_operator_import_confirmation=True)


def _source(tmp_path, name="batch"):
    path = tmp_path / name
    path.mkdir(parents=True)
    (path / "machine.log").write_text("print source", encoding="utf-8")
    return path


def _seed(source, settings, *, status=ImportJobStatus.awaiting_operator_confirmation):
    job = detect_import_candidate(source, settings=settings).job
    job.status = status
    with SessionLocal() as db:
        ImportJobsRepository(db).save_import_job(job)
        db.commit()
    return job


def test_all_candidates_are_prepared_before_the_first_publication(tmp_path, monkeypatch):
    settings = _settings()
    paths = [_source(tmp_path, "first"), _source(tmp_path, "second")]
    for path in paths:
        _seed(path, settings, status=ImportJobStatus.done)
    prepared = []
    original_snapshot = requests.snapshot_source
    original_manifest = requests.calculate_checksum_manifest
    original_publish = requests.publish_import_candidate

    publication_started = False

    @contextmanager
    def publishing_scope():
        nonlocal publication_started
        publication_started = True
        with session_scope() as db:
            yield db

    def snapshot(path):
        assert not publication_started, "local walking must precede SQL publication"
        return original_snapshot(path)

    def manifest(path):
        assert not publication_started, "later candidates must not hash inside the earlier transaction"
        prepared.append(path)
        return original_manifest(path)

    def publish(*args, **kwargs):
        assert prepared == paths
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(requests, "snapshot_source", snapshot)
    monkeypatch.setattr(requests, "calculate_checksum_manifest", manifest)
    monkeypatch.setattr(requests, "publish_import_candidate", publish)
    monkeypatch.setattr(requests, "session_scope", publishing_scope)
    jobs = requests.register_import_candidates(paths, settings=settings)
    assert len(jobs) == 2
    assert publication_started


def test_publication_does_not_touch_files_or_publish_analysis(tmp_path, monkeypatch):
    settings = _settings()
    candidate = requests.prepare_import_candidate(_source(tmp_path), settings=settings)

    def forbidden(*args, **kwargs):
        pytest.fail("request publication must not open files, upload reports or save analytical facts")

    monkeypatch.setattr(requests, "snapshot_source", forbidden)
    monkeypatch.setattr(requests, "calculate_checksum_manifest", forbidden)
    monkeypatch.setattr(RuntimeRepository, "save_sessions", forbidden)
    monkeypatch.setattr(ReportsRepository, "save_prepared", forbidden)
    with SessionLocal() as db:
        result = requests.publish_import_candidate(db, candidate, settings=settings)
        assert result.job.status == ImportJobStatus.awaiting_operator_confirmation
        assert result.notifications
        assert result.sessions == result.reports == {}
        db.rollback()
    with SessionLocal() as db:
        assert ImportJobsRepository(db).count_import_jobs(settings.compute_node_id) == 0


@pytest.mark.parametrize("action, expected", [
    ("confirm", ImportJobStatus.checking_stability),
    ("ignore", ImportJobStatus.ignored),
    ("postpone", ImportJobStatus.postponed),
    ("retry", ImportJobStatus.checking_stability),
])
def test_rest_and_callback_actions_use_the_same_uncommitted_transition(tmp_path, action, expected):
    settings = _settings()
    job = _seed(_source(tmp_path), settings)
    with SessionLocal() as db:
        result = requests.apply_import_callback(
            db, f"import:{job.import_job_id}:{action}", settings=settings, actor="telegram-operator",
        )
        assert result.job.status == expected
        assert result.job.audit_trail[-1]["actor"] in ("telegram-operator", "system")
        db.rollback()
    with SessionLocal() as db:
        assert ImportJobsRepository(db).get_import_job(job.import_job_id).status == job.status


def test_actions_do_not_replace_a_running_lease(tmp_path):
    settings = _settings()
    job = _seed(_source(tmp_path), settings)
    job.lease_owner = "local-worker"
    job.lease_generation = 7
    job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    with SessionLocal() as db:
        ImportJobsRepository(db).save_import_job(job)
        db.commit()
    with SessionLocal() as db:
        with pytest.raises(requests.ImportRequestError, match="already running") as caught:
            requests.apply_import_action(db, job.import_job_id, "ignore", settings=settings)
        assert caught.value.code == "conflict"
        stored = ImportJobsRepository(db).get_import_job(job.import_job_id)
        assert stored.lease_owner == "local-worker"
        assert stored.lease_generation == 7
        assert stored.status == job.status


def test_actions_hide_other_operators_jobs(tmp_path):
    settings = _settings()
    job = _seed(_source(tmp_path), settings)
    other_settings = settings.model_copy(update={"compute_node_id": "other-owner"})
    with SessionLocal() as db:
        with pytest.raises(requests.ImportRequestError) as caught:
            requests.apply_import_action(db, job.import_job_id, "confirm", settings=other_settings)
        assert caught.value.code == "not_found"


@pytest.mark.parametrize("action, expected", [
    ("confirm", "checking_stability"), ("ignore", "ignored"),
    ("postpone", "postponed"), ("retry", "checking_stability"),
])
def test_http_actions_preserve_response_and_workstation_actor(tmp_path, monkeypatch, action, expected):
    settings = get_settings().model_copy(update={"compute_node_id": "request-http-owner"})
    monkeypatch.setattr("api.routes.imports.get_settings", lambda: settings)
    job = _seed(_source(tmp_path), settings)
    response = TestClient(app).post(
        f"/imports/{job.import_job_id}/{action}", json={"actor": "fallback", "retry_seconds": 73},
        headers={"X-Workstation-ID": "operator-mac", "X-API-Token": settings.agent_api_token},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"job", "notifications", "session_ids", "report_ids"}
    assert body["job"]["status"] == expected
    assert body["job"]["owner_node_id"] == settings.compute_node_id
    assert any(item["actor"] == "operator-mac" for item in body["job"]["audit_trail"])


@pytest.mark.parametrize("callback, expected_status, detail", [
    ("broken", 400, "Invalid callback data"),
    ("other:missing:confirm", 400, "Unsupported callback prefix"),
    ("import:missing:confirm", 404, "Import job not found"),
])
def test_callback_http_errors_remain_unchanged(callback, expected_status, detail):
    settings = get_settings()
    response = TestClient(app).post(
        "/agent/import-callback", json={"callback_data": callback},
        headers={"X-API-Token": settings.agent_api_token},
    )
    assert response.status_code == expected_status
    assert response.json() == {"detail": detail}
