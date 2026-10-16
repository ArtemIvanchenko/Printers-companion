"""Accepted attachment workflows keep a current owner-local estimate request."""

import hashlib
import json
from pathlib import Path
import time

import pytest
import trimesh
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from api.main import app
from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from storage.db.session import SessionLocal, engine
from storage.sync.local_outbox import LocalNasOutbox
from worker.estimate_tasks import process_next_estimate
from worker.nas_sync import process_next


class MemoryStore:
    """The only substituted boundary: verified external object transport."""

    objects = {}
    available = True
    on_download = None

    def __init__(self, *args, **kwargs):
        pass

    def is_available(self):
        return type(self).available

    def put_file_verified(self, bucket, name, path, expected_sha256, expected_size, **kwargs):
        payload = Path(path).read_bytes()
        assert len(payload) == expected_size
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        self.objects[bucket, name] = payload
        return f"s3://{bucket}/{name}"

    def download_file(self, bucket, name, destination, expected_sha256=None):
        payload = self.objects[bucket, name]
        assert expected_sha256 == hashlib.sha256(payload).hexdigest()
        Path(destination).write_bytes(payload)
        callback, type(self).on_download = type(self).on_download, None
        if callback:
            callback()
        return Path(destination)


@pytest.fixture
def attachment_client(monkeypatch):
    MemoryStore.objects = {}
    MemoryStore.available = True
    MemoryStore.on_download = None
    monkeypatch.setattr("api.routes.prints.ObjectStore", MemoryStore)
    monkeypatch.setattr("domain.services.estimation.calculation.ObjectStore", MemoryStore)
    monkeypatch.setattr(get_settings(), "nas_sync_retry_min_seconds", 1)
    monkeypatch.setattr(get_settings(), "nas_sync_retry_max_seconds", 1)
    yield TestClient(app)
    MemoryStore.on_download = None


def _card(client, *, configured=True):
    if configured:
        response = client.put("/settings/machine", json={
            "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500,
            "hatch_distance_mm": 0.1, "layer_thickness_mm": 0.5,
            "laser_count": 1, "recoat_time_ms": 9000,
        })
        assert response.status_code == 200, response.text
    response = client.post("/prints", json={"name": "Attachment workflow"})
    assert response.status_code == 200, response.text
    return response.json()["record_id"]


def _stl(client, record_id):
    cube = trimesh.creation.box(extents=[1, 1, 1])
    cube.apply_translation([0, 0, 0.5])
    return _upload(client, record_id, "part.stl", cube.export(file_type="stl"), "stl")


def _upload(client, record_id, name, payload, kind, *, expected_status=200):
    response = client.post(f"/prints/{record_id}/files",
        files={"file": (name, payload, "application/octet-stream")},
        data={"file_type": kind})
    assert response.status_code == expected_status, response.text
    return response.json()


def _jobs(record_id):
    with SessionLocal() as db:
        return [{"job_id": row.job_id, "owner": row.owner_node_id,
                 "status": row.status, "revision": row.payload_json["record_revision"],
                 "payload_owner": row.payload_json["owner_node_id"], "error": row.error,
                 "lease_owner": row.lease_owner, "generation": row.lease_generation,
                 "lease_until": row.lease_until}
                for row in db.scalars(select(BackgroundJob).where(
                    BackgroundJob.entity_type == "print_record",
                    BackgroundJob.entity_id == record_id,
                    BackgroundJob.job_type == "print_estimate",
                ).order_by(BackgroundJob.created_at, BackgroundJob.job_id)).all()]


def _finish(client, record_id, owner):
    while process_next_estimate("attachment-estimator", owner_node_id=owner):
        pass
    current = client.get(f"/prints/{record_id}").json()
    assert current["metadata_json"]["prediction"]["n_parts"] == 1
    assert current["metadata_json"]["prediction"]["input_revision"] == current["revision"] - 1
    assert current["metadata_json"]["prediction"]["computed_by"] == owner
    assert current["prediction_configuration_is_current"] is True
    assert current["estimate_job"] is None
    return current


def _other_attachment(client, record_id, kind, delivery):
    payload = b"synthetic attachment for an actual upload"
    name = "photo.jpg" if kind == "photo" else "drawing.pdf"
    MemoryStore.available = delivery == "http"
    result = _upload(client, record_id, name, payload, kind,
                     expected_status=200 if delivery == "http" else 202)
    MemoryStore.available = True
    if delivery == "nas_sync":
        # Exercise the actual backoff admission rather than rewriting a manifest.
        time.sleep(1.05)
        queue = LocalNasOutbox.from_settings(get_settings())
        state = process_next(outbox=queue, store=MemoryStore(), settings=get_settings(),
                             operation_id=result["sync_operation_id"])
        assert state["status"] == "completed", state
    return payload, name


@pytest.mark.parametrize("kind", ["photo", "doc"])
@pytest.mark.parametrize("delivery", ["http", "nas_sync"])
def test_stl_then_other_attachment_finishes_the_accepted_estimate(attachment_client, kind, delivery):
    client = attachment_client
    owner = get_settings().compute_node_id
    record_id = _card(client)
    _stl(client, record_id)
    original = _jobs(record_id)[0]
    _other_attachment(client, record_id, kind, delivery)
    accepted = client.get(f"/prints/{record_id}").json()
    jobs = _jobs(record_id)
    assert len(jobs) == 2
    assert {job["owner"] for job in jobs} == {owner}
    assert original["revision"] < jobs[-1]["revision"] == accepted["revision"]
    assert jobs[0] == original, "accepting the attachment does not alter the old request/lease"
    current = _finish(client, record_id, owner)
    assert len(current["files"]) == 2
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done"]


@pytest.mark.parametrize("delivery", ["http", "nas_sync"])
def test_foreign_photo_operation_preserves_the_cards_compute_owner(attachment_client, monkeypatch, delivery):
    client = attachment_client
    owner = get_settings().compute_node_id
    record_id = _card(client)
    _stl(client, record_id)
    foreign = owner + "-second-pc"
    with monkeypatch.context() as configured_process:
        configured_process.setattr(get_settings(), "compute_node_id", foreign)
        _other_attachment(client, record_id, "photo", delivery)
    queue = LocalNasOutbox.from_settings(get_settings())
    receipts = [json.loads(path.read_text()) for path in (queue.root / "completed").glob("*.json")]
    photo_receipt = next(receipt for receipt in receipts if receipt["file_type"] == "photo")
    assert photo_receipt["owner_node_id"] == foreign
    assert {job["owner"] for job in _jobs(record_id)} == {owner}
    assert {job["payload_owner"] for job in _jobs(record_id)} == {owner}
    assert not process_next_estimate("foreign-estimator", owner_node_id=foreign)
    current = _finish(client, record_id, owner)
    assert current["origin_compute_node_id"] == owner


def test_running_estimate_cannot_publish_after_photo_but_fresh_request_finishes(attachment_client):
    client = attachment_client
    owner = get_settings().compute_node_id
    record_id = _card(client)
    _stl(client, record_id)
    original_id = _jobs(record_id)[0]["job_id"]

    def upload_during_download():
        running = _jobs(record_id)[0]
        assert running["status"] == "running" and running["lease_owner"]
        _other_attachment(client, record_id, "photo", "http")
        jobs = _jobs(record_id)
        assert len(jobs) == 2
        assert jobs[0] == running, "the existing fenced lease is not replaced"
        assert jobs[1]["status"] == "pending" and jobs[1]["generation"] == 0

    MemoryStore.on_download = upload_during_download
    assert process_next_estimate("attachment-estimator", owner_node_id=owner)
    current = client.get(f"/prints/{record_id}").json()
    assert "prediction" not in current["metadata_json"]
    jobs = _jobs(record_id)
    assert jobs[0]["job_id"] == original_id and jobs[0]["status"] == "failed"
    assert jobs[1]["status"] == "pending"
    _finish(client, record_id, owner)
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done"]


def test_duplicate_other_attachment_does_not_advance_revision_or_request(attachment_client):
    client = attachment_client
    record_id = _card(client)
    _stl(client, record_id)
    payload, name = _other_attachment(client, record_id, "photo", "http")
    before = client.get(f"/prints/{record_id}").json()
    jobs = _jobs(record_id)
    assert len(jobs) == 2
    duplicate = _upload(client, record_id, name, payload, "photo")
    assert duplicate["duplicate"] is True
    after = client.get(f"/prints/{record_id}").json()
    assert after["revision"] == before["revision"]
    assert after["files"] == before["files"]
    assert _jobs(record_id) == jobs


@pytest.mark.parametrize("after_completed_estimate", [False, True])
def test_photo_without_an_active_workflow_does_not_start_compute(attachment_client, after_completed_estimate):
    client = attachment_client
    owner = get_settings().compute_node_id
    record_id = _card(client)
    if after_completed_estimate:
        _stl(client, record_id)
        _finish(client, record_id, owner)
    jobs = _jobs(record_id)
    _other_attachment(client, record_id, "photo", "http")
    assert _jobs(record_id) == jobs
    assert not process_next_estimate("attachment-estimator", owner_node_id=owner)


def test_missing_parameters_report_failure_for_the_current_accepted_request(attachment_client):
    client = attachment_client
    owner = get_settings().compute_node_id
    record_id = _card(client, configured=False)
    _stl(client, record_id)
    _other_attachment(client, record_id, "doc", "http")
    accepted = client.get(f"/prints/{record_id}").json()
    assert len(_jobs(record_id)) == 2
    assert process_next_estimate("attachment-estimator", owner_node_id=owner)
    assert process_next_estimate("attachment-estimator", owner_node_id=owner)
    assert not process_next_estimate("attachment-estimator", owner_node_id=owner)
    jobs = _jobs(record_id)
    assert jobs[-1]["revision"] == accepted["revision"]
    assert [job["status"] for job in jobs] == ["failed", "failed"]
    assert "параметров машины" in jobs[-1]["error"]
    current = client.get(f"/prints/{record_id}").json()
    assert current["estimate_job"] is None
    assert "prediction" not in current["metadata_json"]
    assert len(current["files"]) == 2


@pytest.mark.parametrize("delivery", ["http", "nas_sync"])
def test_new_request_sql_failure_rolls_back_attachment_and_revision(attachment_client, delivery):
    client = attachment_client
    record_id = _card(client)
    _stl(client, record_id)
    before = client.get(f"/prints/{record_id}").json()
    original_jobs = _jobs(record_id)
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TRIGGER reject_continuity_request BEFORE INSERT ON background_jobs
            BEGIN SELECT RAISE(ABORT, 'synthetic estimate request rejection'); END
        """)
    try:
        if delivery == "http":
            with pytest.raises(IntegrityError):
                _other_attachment(client, record_id, "photo", "http")
        else:
            MemoryStore.available = False
            accepted = _upload(client, record_id, "photo.jpg", b"pending photo", "photo",
                               expected_status=202)
            MemoryStore.available = True
            time.sleep(1.05)
            queue = LocalNasOutbox.from_settings(get_settings())
            state = process_next(outbox=queue, store=MemoryStore(), settings=get_settings(),
                                 operation_id=accepted["sync_operation_id"])
            assert state["status"] == "failed"
    finally:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER reject_continuity_request")
    after = client.get(f"/prints/{record_id}").json()
    assert after["revision"] == before["revision"]
    assert after["files"] == before["files"]
    assert _jobs(record_id) == original_jobs
    queue = LocalNasOutbox.from_settings(get_settings())
    assert queue.status()["counts"]["failed"] == 1
