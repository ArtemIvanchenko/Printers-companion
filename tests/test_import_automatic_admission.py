"""All intake paths apply the saved approval policy before the owner claims work."""

import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
import pytest
from psycopg.errors import ConnectionFailure
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app, _startup_import
from core.config.settings import get_settings
from domain.models.events import LayerSnapshot
from domain.models.sessions import BuildSession, ImportJob, ReportArtifact
from domain.services.importing.recovery import recover_upload_receipt
from storage.db.session import SessionLocal
from storage.repositories.import_jobs import ImportJobsRepository
from worker.tasks import process_due_import_jobs
from worker import watcher


LOG_NAME = "01.04.2026_time.log"
LOG_DATA = b"OLD_STATS: 1|9000|30000|39300|\nOLD_STATS: 2|9000|31000|40300|\n"


@pytest.fixture
def intake(tmp_path, monkeypatch):
    settings = get_settings()
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(raw))
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    monkeypatch.setattr(settings, "file_stability_retry_seconds", 1)
    return TestClient(app), raw, settings


def _submit(intake, entry, monkeypatch):
    client, raw, settings = intake
    if entry in {"browser", "card_browser"}:
        endpoint = "/upload/logs"
        if entry == "card_browser":
            response = client.post("/prints", json={"name": "Automatic intake"})
            assert response.status_code == 200, response.text
            endpoint = f"/prints/{response.json()['record_id']}/import-logs"
        response = client.post(endpoint, files=[("files", (LOG_NAME, LOG_DATA, "text/plain"))])
        assert response.status_code == 200, response.text
    else:
        (raw / LOG_NAME).write_bytes(LOG_DATA)
        if entry == "rescan":
            response = client.post("/upload/rescan")
            assert response.status_code == 200, response.text
        elif entry == "agent":
            response = client.post("/agent/import-detected", json={"source_path": str(raw)},
                                   headers={"X-API-Token": settings.agent_api_token})
            assert response.status_code == 200, response.text
        elif entry == "startup":
            _startup_import(str(raw))
        elif entry == "watcher":
            paths = []

            class ApiTransport:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def post(self, url, **kwargs):
                    paths.append(urlsplit(url).path)
                    return client.post(paths[-1], **kwargs)

            with monkeypatch.context() as transport:
                transport.setattr(watcher.httpx, "Client", lambda **kwargs: ApiTransport())
                watcher.scan_existing_candidates(raw, watcher.CandidateTracker())
            assert paths == ["/agent/import-detected"], "approval belongs to the shared registration"
        else:
            raise AssertionError(entry)
    with SessionLocal() as db:
        jobs = list(db.scalars(select(ImportJob)).all())
        assert len(jobs) == 1
        return ImportJobsRepository(db).get_import_job(jobs[0].import_job_id)


def _assert_finished(job, settings, *, expected_generation=1):
    assert process_due_import_jobs("automatic-intake-worker") == 1
    with SessionLocal() as db:
        saved = db.get(ImportJob, job.import_job_id)
        assert saved.status == "done", saved.error
        assert saved.owner_node_id == settings.compute_node_id
        assert saved.lease_owner is None and saved.lease_generation == expected_generation
        assert len(saved.session_ids) == len(saved.report_ids) == 1
        session = db.get(BuildSession, saved.session_ids[0])
        manifest = session.context["timing_publication"]
        assert manifest["source"] == {"import_job_id": saved.import_job_id,
                                     "owner_node_id": settings.compute_node_id,
                                     "lease_generation": expected_generation}
        rows = list(db.scalars(select(LayerSnapshot).where(
            LayerSnapshot.session_id == saved.session_ids[0])).all())
        assert len(rows) == 2
        assert db.get(ReportArtifact, saved.report_ids[0]).payload["timing_publication"] == manifest
        assert (Path(saved.source_path) / LOG_NAME).read_bytes() == LOG_DATA


@pytest.mark.parametrize("entry", ["browser", "card_browser", "rescan", "agent", "startup", "watcher"])
@pytest.mark.parametrize("require_confirmation", [False, True])
def test_every_registration_obeys_approval_policy_and_uses_the_actual_owner_worker(
    intake, monkeypatch, entry, require_confirmation,
):
    _, _, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", require_confirmation)
    job = _submit(intake, entry, monkeypatch)
    if require_confirmation:
        assert job.status == "awaiting_operator_confirmation"
        assert job.confirmed_by is None and job.confirmed_at is None
        assert process_due_import_jobs("automatic-intake-worker") == 0
        with SessionLocal() as db:
            assert not list(db.scalars(select(BuildSession)).all())
            assert not list(db.scalars(select(ReportArtifact)).all())
    else:
        assert job.status == "checking_stability"
        assert job.confirmed_by == "automatic_policy" and job.confirmed_at
        assert any(row["action"] == "queued_for_worker" for row in job.audit_trail)
        owner = settings.compute_node_id
        with monkeypatch.context() as foreign_process:
            foreign_process.setattr(settings, "compute_node_id", owner + "-other")
            assert process_due_import_jobs("foreign-intake-worker") == 0
        _assert_finished(job, settings)


@pytest.mark.parametrize("require_confirmation", [False, True])
def test_explicit_ingest_publishes_the_new_job_even_if_detection_already_queued_it(
    intake, monkeypatch, require_confirmation,
):
    client, raw, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", require_confirmation)
    (raw / LOG_NAME).write_bytes(LOG_DATA)
    response = client.post("/sessions/ingest", json={"folder": str(raw)})
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        job = ImportJobsRepository(db).get_import_job(response.json()["job_id"])
        assert job is not None and job.status == "checking_stability"
        assert job.confirmed_by == "operator"
    _assert_finished(job, settings)


def test_automatic_intake_remains_claimable_after_stability_postponement(intake, monkeypatch):
    _, _, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", False)
    monkeypatch.setattr(settings, "file_stability_seconds", 60)
    job = _submit(intake, "browser", monkeypatch)
    assert process_due_import_jobs("automatic-intake-worker") == 1
    with SessionLocal() as db:
        postponed = db.get(ImportJob, job.import_job_id)
        assert postponed.status == "postponed"
        assert postponed.confirmed_by == "automatic_policy"
        assert postponed.stability_check_attempts == 1
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    time.sleep(1.05)
    _assert_finished(job, settings, expected_generation=2)


def _failed_upload_receipt(intake, monkeypatch):
    client, raw, _ = intake
    def failed_commit(self):
        raise OperationalError("COMMIT", {}, ConnectionFailure("synthetic NAS disconnect"))
    with monkeypatch.context() as disconnected:
        disconnected.setattr(Session, "commit", failed_commit)
        response = client.post("/upload/logs", files=[("files", (LOG_NAME, LOG_DATA, "text/plain"))])
        assert response.status_code == 503, response.text
    receipt_path = next((raw / "incoming").glob("*.json"))
    return receipt_path, json.loads(receipt_path.read_text())


@pytest.mark.parametrize("initial_confirmation", [False, True])
def test_receipt_recovery_uses_the_original_policy_after_settings_change(
    intake, monkeypatch, initial_confirmation,
):
    _, _, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", initial_confirmation)
    receipt_path, receipt = _failed_upload_receipt(intake, monkeypatch)
    monkeypatch.setattr(settings, "require_operator_import_confirmation", not initial_confirmation)
    assert recover_upload_receipt(receipt_path, settings=settings)["state"] == "registered"
    with SessionLocal() as db:
        job = ImportJobsRepository(db).get_import_job(receipt["import_job_id"])
        assert job.confirmed_by == (None if initial_confirmation else "automatic_policy")
        assert job.status == ("awaiting_operator_confirmation" if initial_confirmation else "checking_stability")
    if initial_confirmation:
        assert process_due_import_jobs("automatic-intake-worker") == 0
    else:
        _assert_finished(job, settings)


@pytest.mark.parametrize("field", ["confirmed_by", "confirmed_at"])
def test_queued_receipt_requires_its_recorded_automatic_approval(intake, monkeypatch, field):
    _, _, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", False)
    receipt_path, receipt = _failed_upload_receipt(intake, monkeypatch)
    receipt["job"][field] = None
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert recover_upload_receipt(receipt_path, settings=settings)["state"] == "needs_attention"
    with SessionLocal() as db:
        assert not list(db.scalars(select(ImportJob)).all())
    assert (Path(receipt["source_path"]) / LOG_NAME).read_bytes() == LOG_DATA


def test_legacy_v2_detected_receipt_is_not_reinterpreted_as_automatic_approval(intake, monkeypatch):
    _, _, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", False)
    receipt_path, receipt = _failed_upload_receipt(intake, monkeypatch)
    receipt["job"].update(status="detected", confirmed_by=None, confirmed_at=None)
    receipt["job"]["audit_trail"] = [row for row in receipt["job"]["audit_trail"]
                                     if row["action"] == "detected"]
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    assert recover_upload_receipt(receipt_path, settings=settings)["state"] == "registered"
    with SessionLocal() as db:
        job = db.get(ImportJob, receipt["import_job_id"])
        assert job.status == "detected" and job.confirmed_by is None
    assert process_due_import_jobs("automatic-intake-worker") == 0


def test_redetection_does_not_replace_a_running_automatic_lease_or_policy(intake, monkeypatch):
    client, raw, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", False)
    job = _submit(intake, "agent", monkeypatch)
    with SessionLocal() as db:
        claimed = ImportJobsRepository(db).claim_next_import_job(
            owner_node_id=settings.compute_node_id, lease_owner="claimed-automatic-worker")
        db.commit()
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    response = client.post("/agent/import-detected", json={"source_path": str(raw)},
                           headers={"X-API-Token": settings.agent_api_token})
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        saved = ImportJobsRepository(db).get_import_job(job.import_job_id)
        assert saved.status == claimed.status
        assert saved.lease_owner == claimed.lease_owner
        assert saved.lease_generation == claimed.lease_generation
        assert saved.confirmed_by == claimed.confirmed_by == "automatic_policy"
        assert saved.confirmed_at == claimed.confirmed_at


def test_redetection_cannot_approve_a_saved_manual_request(intake, monkeypatch):
    client, raw, settings = intake
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    job = _submit(intake, "agent", monkeypatch)
    monkeypatch.setattr(settings, "require_operator_import_confirmation", False)
    response = client.post("/agent/import-detected", json={"source_path": str(raw)},
                           headers={"X-API-Token": settings.agent_api_token})
    assert response.status_code == 200, response.text
    assert response.json()["job"]["import_job_id"] == job.import_job_id
    assert response.json()["job"]["status"] == "awaiting_operator_confirmation"
    assert process_due_import_jobs("automatic-intake-worker") == 0
