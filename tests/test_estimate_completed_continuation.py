"""Completed successors fulfil older accepted goals without new computation.

API publication and one-owner consumers stay native. Recovery edge tests below
advance only the jobs repository clock; the separate native audit waited the
real 60-second TTL without clock, lease, payload or status edits.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
import trimesh

from api.main import app
from core.config.settings import get_settings
from core.versioning.constants import ANALYSIS_VERSION
from domain.models.jobs import BackgroundJob
from domain.models.prints import PrintRecord, PrintRecordFile
from core.versioning.provenance import stable_hash
from domain.services.estimation.inputs import (
    prepare_prediction_inputs, prediction_input_hash, prediction_configuration,
)
from storage.db.session import SessionLocal, session_scope
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.prints_repo import PrintsRepository
from worker.estimate_tasks import process_next_estimate


class MemoryStore:
    """Only external object transport is substituted, with verified SHA."""
    objects = {}
    on_download = None

    def __init__(self, *args, **kwargs):
        self.settings = get_settings()

    def is_available(self):
        return True

    def put_file_verified(self, bucket, name, path, expected_sha256, expected_size, **kwargs):
        payload = Path(path).read_bytes()
        assert len(payload) == expected_size
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        self.objects[bucket, name] = payload
        return f"s3://{bucket}/{name}"

    def download_file(self, bucket, name, destination, expected_sha256=None):
        payload = self.objects[bucket, name]
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        Path(destination).write_bytes(payload)
        callback, type(self).on_download = type(self).on_download, None
        if callback:
            callback()
        return Path(destination)


def _jobs(record_id):
    with SessionLocal() as db:
        repo = JobsRepository(db)
        return [repo.get(row.job_id) for row in db.scalars(select(BackgroundJob).where(
            BackgroundJob.job_type == "print_estimate", BackgroundJob.entity_id == record_id,
        ).order_by(BackgroundJob.created_at, BackgroundJob.job_id))]


def _record(record_id):
    with SessionLocal() as db:
        return PrintsRepository(db).get_print_record(record_id)


@pytest.fixture
def accepted(monkeypatch):
    MemoryStore.objects = {}
    MemoryStore.on_download = None
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", MemoryStore)
    monkeypatch.setattr("api.routes.prints.ObjectStore", MemoryStore)
    monkeypatch.setattr("domain.services.estimation.calculation.ObjectStore", MemoryStore)
    client = TestClient(app)
    response = client.put("/settings/machine", json={
        "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500, "hatch_distance_mm": .1,
        "layer_thickness_mm": .5, "laser_count": 1, "recoat_time_ms": 9000,
    })
    assert response.status_code == 200, response.text
    response = client.post("/prints", json={"name": "Already fulfilled accepted goal"})
    assert response.status_code == 200, response.text
    record_id = response.json()["record_id"]
    cube = trimesh.creation.box(extents=[1, 1, 1])
    cube.apply_translation([0, 0, .5])
    response = client.post(f"/prints/{record_id}/files",
        files={"file": ("part.stl", cube.export(file_type="stl"), "application/octet-stream")},
        data={"file_type": "stl"})
    assert response.status_code == 200, response.text
    file_id = response.json()["file_id"]
    yield client, record_id, file_id
    MemoryStore.on_download = None
    client.close()


@pytest.fixture
def fulfilled(accepted):
    client, record_id, file_id = accepted
    with session_scope() as db:
        old = JobsRepository(db).claim_next("print_estimate", owner_node_id=get_settings().compute_node_id,
            lease_owner="stopped-consumer", lease_seconds=60)
    assert old is not None and old["payload"]["record_revision"] == 2
    response = client.post(f"/prints/{record_id}/files",
        files={"file": ("operator.txt", b"Operator setup confirmed", "text/plain")},
        data={"file_type": "doc"})
    assert response.status_code == 200, response.text
    assert [job["status"] for job in _jobs(record_id)] == ["running", "pending"]
    assert process_next_estimate("fresh-consumer")
    record = client.get(f"/prints/{record_id}").json()
    prediction = record["metadata_json"]["prediction"]
    assert record["revision"] == 4 and prediction["input_revision"] == 3
    assert record["prediction_is_current"] and record["prediction_configuration_is_current"]
    assert [job["status"] for job in _jobs(record_id)] == ["running", "done"]
    yield client, record_id, file_id, old, prediction


def _advance_recovery_clock(monkeypatch, old):
    # Controlled repository-clock fixture, NOT evidence of a naturally elapsed
    # lease. The running row and captured payload/status are never rewritten.
    until = datetime.fromisoformat(old["lease_until"])
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    recovery_time = until + timedelta(seconds=1)
    monkeypatch.setattr("storage.repositories.jobs_repo._now", lambda: recovery_time)
    return recovery_time


def _recover(monkeypatch, old):
    _advance_recovery_clock(monkeypatch, old)
    assert process_next_estimate("recovery-consumer")


def test_fulfilled_successor_retires_old_request_without_recomputing(fulfilled, monkeypatch):
    client, record_id, _, old, prediction = fulfilled
    with SessionLocal() as db:
        current = prepare_prediction_inputs(PrintsRepository(db), record_id,
                                             compute_node_id=get_settings().compute_node_id)
    normalized = deepcopy(current)
    normalized["record"]["revision"] = prediction["input_revision"]
    assert prediction_input_hash(normalized) == prediction["input_hash"]
    assert prediction_input_hash(current) != prediction["input_hash"]
    done_before = _jobs(record_id)[1]
    _recover(monkeypatch, old)
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "done"]
    assert jobs[0]["payload"] == old["payload"]
    assert jobs[0]["lease_generation"] == 2 and jobs[0]["attempts"] == 2
    assert jobs[0]["result"] == {} and jobs[0]["lease_owner"] is None
    assert jobs[1] == done_before
    assert _record(record_id)["revision"] == current["record"]["revision"] == 4
    assert _record(record_id)["metadata_json"]["prediction"] == prediction
    card = client.get(f"/prints/{record_id}").json()
    assert card["estimate_job"] is None
    assert card["prediction_is_current"] and card["prediction_configuration_is_current"]
    assert not process_next_estimate("no-active-consumer")


def test_successor_completed_during_original_computation_also_fulfills_goal(accepted):
    client, record_id, _ = accepted
    original = _jobs(record_id)[0]
    seen = {}
    def fresh_successor():
        response = client.post(f"/prints/{record_id}/files",
            files={"file": ("operator.txt", b"Operator context during calculation", "text/plain")},
            data={"file_type": "doc"})
        assert response.status_code == 200, response.text
        assert process_next_estimate("fresh-concurrent-consumer")
        seen["record"] = _record(record_id)
        assert seen["record"]["revision"] == 4
        assert [job["status"] for job in _jobs(record_id)] == ["running", "done"]
    MemoryStore.on_download = fresh_successor
    assert process_next_estimate("original-consumer")
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "done"]
    assert jobs[0]["payload"] == original["payload"] and jobs[0]["lease_generation"] == 1
    assert _record(record_id) == seen["record"]
    assert not process_next_estimate("no-redundant-consumer")


def test_failed_retirement_commit_rolls_back_fulfilled_transition(fulfilled, monkeypatch):
    _, record_id, _, old, prediction = fulfilled
    native = Session.commit
    def reject_retirement(db):
        row = db.get(BackgroundJob, old["job_id"])
        if row is not None and row.status == "failed":
            raise OperationalError("COMMIT", {}, RuntimeError("controlled continuation commit failure"))
        return native(db)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", reject_retirement)
        with pytest.raises(OperationalError, match="controlled continuation commit failure"):
            _recover(patch, old)
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["running", "done"]
    assert jobs[0]["lease_generation"] == 2 and jobs[0]["lease_owner"] == "recovery-consumer"
    assert jobs[0]["payload"] == old["payload"]
    assert _record(record_id)["revision"] == 4
    assert _record(record_id)["metadata_json"]["prediction"] == prediction
    _recover(monkeypatch, jobs[0])
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done"]
    assert _jobs(record_id)[0]["lease_generation"] == 3


@pytest.mark.parametrize("change", ["settings", "geometry_checksum", "powder", "physical_printer"])
def test_current_input_difference_still_enqueues_fresh_request(fulfilled, monkeypatch, change):
    client, record_id, file_id, old, prediction = fulfilled
    if change == "settings":
        response = client.put("/settings/machine", json={"recoat_time_ms": 11000})
        assert response.status_code == 200, response.text
    else:
        # Controlled persisted-input edits hold card revision fixed so these
        # negative guards test the FULL hash, independently of revision checks.
        with SessionLocal() as db:
            if change == "geometry_checksum":
                row = db.get(PrintRecordFile, file_id)
                cube = trimesh.creation.box(extents=[2, 1, 1])
                cube.apply_translation([0, 0, .5])
                payload = cube.export(file_type="stl")
                bucket, name = row.object_uri.removeprefix("s3://").split("/", 1)
                MemoryStore.objects[bucket, name] = payload
                row.checksum = hashlib.sha256(payload).hexdigest()
                row.size_bytes = len(payload)
            else:
                row = db.get(PrintRecord, record_id)
                if change == "powder":
                    values = {"powder_cost_rub_per_kg": 32100}
                else:
                    values = {"metadata_json": {**row.metadata_json, "printer_id": "different-physical-printer"}}
                db.execute(update(PrintRecord.__table__).where(PrintRecord.record_id == record_id).values(**values))
            db.commit()
    assert _record(record_id)["revision"] == prediction["input_revision"] + 1
    _recover(monkeypatch, old)
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "done", "pending"]
    assert jobs[0]["payload"] == old["payload"]
    assert jobs[2]["payload"]["record_revision"] == 4
    assert jobs[2]["payload"]["manual_rerun"] is False
    assert process_next_estimate("fresh-replacement-consumer")
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done", "done"]
    assert _record(record_id)["revision"] == 5


def test_newer_explicit_manual_acceptance_is_not_fulfilled_by_old_quote(fulfilled, monkeypatch):
    client, record_id, _, old, prediction = fulfilled
    _recover(monkeypatch, old)
    response = client.post(f"/prints/{record_id}/estimate")
    assert response.status_code == 200, response.text
    manual = _jobs(record_id)[-1]
    assert manual["job_id"] == response.json()["job_id"]
    assert manual["payload"]["manual_rerun"] is True
    assert manual["payload"]["record_revision"] > prediction["input_revision"]
    response = client.patch(f"/prints/{record_id}", json={"expected_revision": 4,
        "notes": "More operator context after explicit manual acceptance"})
    assert response.status_code == 200, response.text
    assert process_next_estimate("manual-recovery-consumer")
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "done", "failed", "pending"]
    assert jobs[2]["payload"] == manual["payload"]
    assert jobs[3]["payload"]["record_revision"] == 5
    assert process_next_estimate("manual-successor-consumer")
    assert _record(record_id)["revision"] == 6


@pytest.mark.parametrize("capture", [
    None, "legacy", [], {},
    {"input_revision": None}, {"input_revision": 0}, {"input_revision": -1},
    {"input_revision": True}, {"input_revision": "3"}, {"input_revision": 3.0},
    {"input_revision": 2}, {"input_revision": 4},
    {"input_hash": None}, {"input_hash": "corrupted"}, {"input_hash": []},
    {"calculation_inputs": None}, {"calculation_inputs": {}}, {"calculation_inputs": []},
    {"calculation_inputs": {"configuration_schema_version": True}},
    {"calculation_inputs": {"configuration_schema_version": "1"}},
    {"provenance": None}, {"provenance": {}}, {"provenance": []},
    {"provenance": {"component": "other-component", "config_hash": "corrupted"}},
    {"provenance": {"component": "print-estimate", "config_hash": "corrupted"}},
])
def test_missing_or_corrupt_capture_cannot_suppress_current_request(fulfilled, monkeypatch, capture):
    _, record_id, _, old, prediction = fulfilled
    with SessionLocal() as db:
        row = db.get(PrintRecord, record_id)
        saved = {**prediction, **capture} if isinstance(capture, dict) and capture else capture
        # Capture-corruption guard, deliberately independent of the optimistic
        # revision increment performed by an ordinary ORM/card edit.
        db.execute(update(PrintRecord.__table__).where(PrintRecord.record_id == record_id).values(
            metadata_json={**row.metadata_json, "prediction": saved}))
        db.commit()
    _recover(monkeypatch, old)
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done", "pending"]
    assert _record(record_id)["revision"] == 4


@pytest.mark.parametrize("change", ["old_analysis", "old_configuration"])
def test_other_captured_calculation_generation_does_not_fulfill_goal(fulfilled, monkeypatch, change):
    _, record_id, _, old, prediction = fulfilled
    with SessionLocal() as db:
        repo = PrintsRepository(db)
        current = prepare_prediction_inputs(repo, record_id, compute_node_id=get_settings().compute_node_id)
        configuration = prediction_configuration(current)
        assert configuration["analysis_version"] == prediction["analysis_version"] == ANALYSIS_VERSION
        saved = deepcopy(prediction)
        if change == "old_analysis":
            configuration["analysis_version"] = "0.5.7"
            assert configuration["analysis_version"] != prediction["analysis_version"]
            saved["analysis_version"] = configuration["analysis_version"]
            saved["provenance"]["analysis_version"] = configuration["analysis_version"]
        else:
            configuration["params"]["recoat_time_ms"] = 11000
        saved["provenance"]["config_hash"] = stable_hash(configuration)
        assert saved["input_hash"] == prediction["input_hash"]
        db.execute(update(PrintRecord.__table__).where(PrintRecord.record_id == record_id).values(
            metadata_json={**current["record"]["metadata_json"], "prediction": saved}))
        db.commit()
    _recover(monkeypatch, old)
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done", "pending"]
    assert _record(record_id)["revision"] == 4


def test_unknown_publication_transition_does_not_normalize_more_revisions(fulfilled, monkeypatch):
    client, record_id, _, old, _ = fulfilled
    response = client.patch(f"/prints/{record_id}", json={"expected_revision": 4,
        "notes": "Revision after the completed publication"})
    assert response.status_code == 200, response.text
    _recover(monkeypatch, old)
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "done", "pending"]
    assert jobs[-1]["payload"]["record_revision"] == 5


def test_foreign_consumer_cannot_retire_fulfilled_goal(fulfilled, monkeypatch):
    _, record_id, _, old, prediction = fulfilled
    _advance_recovery_clock(monkeypatch, old)
    before = _jobs(record_id)
    assert not process_next_estimate("foreign-consumer", owner_node_id="foreign-node")
    assert _jobs(record_id) == before
    assert _record(record_id)["metadata_json"]["prediction"] == prediction


def test_foreign_current_card_cannot_use_fulfilled_capture_as_permission(fulfilled, monkeypatch):
    _, record_id, _, old, prediction = fulfilled
    with SessionLocal() as db:
        db.get(PrintRecord, record_id).origin_compute_node_id = "foreign-node"
        db.commit()
    _recover(monkeypatch, old)
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done"]
    assert _record(record_id)["metadata_json"]["prediction"] == prediction


def test_reclaimed_fence_before_continuation_cannot_retire_or_requeue(fulfilled, monkeypatch):
    _, record_id, _, old, prediction = fulfilled
    recovery_time = _advance_recovery_clock(monkeypatch, old)
    import worker.estimate_tasks as worker
    native = worker._continue_stale_estimate
    def reclaimed(job, error, **kwargs):
        # Controlled second crash/reclaim occurs before the continuation locks;
        # use the real claim operation, never overwrite the payload or status.
        with session_scope() as db:
            replacement = JobsRepository(db).claim_next("print_estimate",
                owner_node_id=get_settings().compute_node_id, lease_owner="new-generation-consumer",
                now=recovery_time + timedelta(seconds=get_settings().job_lease_seconds + 1))
        assert replacement["job_id"] == old["job_id"] and replacement["lease_generation"] == 3
        return native(job, error, **kwargs)
    monkeypatch.setattr(worker, "_continue_stale_estimate", reclaimed)
    assert process_next_estimate("recovery-consumer")
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["running", "done"]
    assert jobs[0]["lease_generation"] == 3
    assert jobs[0]["lease_owner"] == "new-generation-consumer"
    assert jobs[0]["payload"] == old["payload"]
    assert _record(record_id)["revision"] == 4
    assert _record(record_id)["metadata_json"]["prediction"] == prediction
