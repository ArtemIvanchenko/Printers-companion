"""Content deduplication and an attachment's lifetime have distinct identities."""

import hashlib
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
import trimesh

from api.main import app
from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from storage.db.session import SessionLocal
from storage.sync.local_outbox import LocalNasOutbox
from worker.estimate_tasks import process_next_estimate
from worker.nas_sync import process_next


def _enqueue(queue, source, *, reopen_completed=False):
    payload = source.read_bytes()
    return queue.enqueue_attachment(
        source, owner_node_id="lifetime-operator", record_id="pr_lifetime",
        file_name=source.name, file_type="doc", bucket="docs",
        checksum=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload),
        reopen_completed=reopen_completed,
    )


def _manifest(queue, operation_id):
    state = queue.get(operation_id)["status"]
    path = (queue.root / "completed" / f"{operation_id}.json" if state == "completed"
            else queue.root / state / operation_id / "manifest.json")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def queued_attachment(tmp_path):
    source = tmp_path / "drawing.txt"
    source.write_bytes(b"immutable drawing")
    queue = LocalNasOutbox(tmp_path / "queue", max_bytes=10_000,
                           retry_min_seconds=1, retry_max_seconds=1, claim_seconds=60)
    operation = _enqueue(queue, source)
    return queue, source, operation["operation_id"]


def test_separate_manifest_admissions_have_fresh_ids_and_the_same_content_operation(queued_attachment, tmp_path):
    queue, source, operation_id = queued_attachment
    other = LocalNasOutbox(tmp_path / "other-queue", max_bytes=10_000)
    admitted = _enqueue(other, source)
    first, second = _manifest(queue, operation_id), _manifest(other, operation_id)
    assert admitted["operation_id"] == operation_id
    assert first["checksum"] == second["checksum"]
    assert first["file_id"] != second["file_id"]
    assert re.fullmatch(r"prf_sync_[0-9a-f]{32}", first["file_id"])


@pytest.mark.parametrize("state", ["pending", "processing", "released", "recovered", "failed"])
def test_duplicate_and_recovery_keep_the_persisted_lifetime_id(queued_attachment, monkeypatch, state):
    queue, source, operation_id = queued_attachment
    file_id = _manifest(queue, operation_id)["file_id"]
    if state != "pending":
        claimed = queue.claim(operation_id)
        assert claimed["file_id"] == file_id
        if state == "released":
            assert queue.release(operation_id, "temporary outage", claim_token=claimed["claim_token"])
        elif state == "recovered":
            # Controlled recovery clock, not a claim of natural elapsed TTL.
            future = time.time() + 120
            with monkeypatch.context() as controlled:
                controlled.setattr("storage.sync.local_outbox.time", SimpleNamespace(time=lambda: future))
                assert queue.status()["counts"]["pending"] == 1
        elif state == "failed":
            assert queue.fail(operation_id, "review required", claim_token=claimed["claim_token"])

    before = _manifest(queue, operation_id)
    # A new process and browser retry must use the existing immutable manifest.
    reopened_process = LocalNasOutbox(queue.root, max_bytes=10_000,
                                     retry_min_seconds=1, retry_max_seconds=1, claim_seconds=60)
    repeated = _enqueue(reopened_process, source, reopen_completed=True)
    assert repeated["operation_id"] == operation_id
    assert _manifest(reopened_process, operation_id) == before
    assert before["file_id"] == file_id
    if state == "processing":
        assert queue.release(operation_id, "retry", claim_token=claimed["claim_token"])
    elif state == "failed":
        assert reopened_process.retry_failed(operation_id)
    future = time.time() + 120
    with monkeypatch.context() as controlled:
        controlled.setattr("storage.sync.local_outbox.time", SimpleNamespace(time=lambda: future))
        retry = reopened_process.claim(operation_id)
    assert retry["file_id"] == file_id
    assert reopened_process.verify_claimed(retry).read_bytes() == source.read_bytes()


def test_completed_receipt_is_stable_but_explicit_new_admission_gets_a_fresh_id(queued_attachment):
    queue, source, operation_id = queued_attachment
    first = queue.claim(operation_id)
    result = {"file_id": first["file_id"], "object_uri": "s3://docs/original"}
    assert queue.complete(operation_id, result, claim_token=first["claim_token"])
    receipt = _manifest(queue, operation_id)
    repeated = _enqueue(queue, source)
    assert repeated["status"] == "completed" and repeated["result"] == result
    assert _manifest(queue, operation_id) == receipt

    replacement = _enqueue(queue, source, reopen_completed=True)
    second = _manifest(queue, operation_id)
    assert replacement["operation_id"] == operation_id and replacement["status"] == "pending"
    assert second["schema_version"] == receipt["schema_version"] == 1
    assert second["checksum"] == receipt["checksum"]
    assert second["file_id"] != first["file_id"]
    assert _enqueue(queue, source, reopen_completed=True) == replacement
    assert _manifest(queue, operation_id) == second
    assert queue.claim(operation_id)["file_id"] == second["file_id"]


def test_legacy_deterministic_manifest_and_receipt_remain_readable(queued_attachment):
    queue, source, operation_id = queued_attachment
    path = queue.root / "pending" / operation_id / "manifest.json"
    manifest = _manifest(queue, operation_id)
    legacy_id = f"prf_sync_{operation_id[:48]}"
    manifest.update(file_id=legacy_id, object_name=f"pr_lifetime/{legacy_id}_{manifest['checksum']}")
    queue._write_json(path, manifest)  # A persisted schema-v1 legacy fixture.
    assert _enqueue(queue, source, reopen_completed=True)["operation_id"] == operation_id
    assert _manifest(queue, operation_id) == manifest
    claim = queue.claim(operation_id)
    assert claim["file_id"] == legacy_id
    legacy_uri = f"s3://docs/{manifest['object_name']}"
    result = {"file_id": legacy_id, "object_uri": legacy_uri}
    assert queue.complete(operation_id, result, claim_token=claim["claim_token"])
    receipt = _manifest(queue, operation_id)
    assert _enqueue(queue, source)["result"] == result
    assert _manifest(queue, operation_id) == receipt


class MemoryStore:
    """Only external blob transport is replaced; API, SQL and STL math are real."""

    objects = {}
    puts = 0
    on_download = None

    def __init__(self, *args, **kwargs):
        pass

    def is_available(self):
        return True

    def put_file_verified(self, bucket, name, path, expected_sha256, expected_size, **kwargs):
        payload = Path(path).read_bytes()
        assert len(payload) == expected_size
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        type(self).puts += 1
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

    def remove_object(self, bucket, name):
        self.objects.pop((bucket, name), None)
        return True


@pytest.fixture
def lifetime_client(monkeypatch):
    MemoryStore.objects, MemoryStore.puts, MemoryStore.on_download = {}, 0, None
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", MemoryStore)
    monkeypatch.setattr("api.routes.prints.ObjectStore", MemoryStore)
    monkeypatch.setattr("domain.services.estimation.calculation.ObjectStore", MemoryStore)
    monkeypatch.setattr(get_settings(), "nas_sync_retry_min_seconds", 1)
    monkeypatch.setattr(get_settings(), "nas_sync_retry_max_seconds", 1)
    client = TestClient(app)  # Keep API lifespan/startup discovery outside this harness.
    try:
        response = client.put("/settings/machine", json={
            "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500, "hatch_distance_mm": .1,
            "layer_thickness_mm": .5, "laser_count": 1, "recoat_time_ms": 9000,
        })
        assert response.status_code == 200, response.text
        yield client
    finally:
        MemoryStore.on_download = None
        client.close()


def _card(client, record_id):
    response = client.get(f"/prints/{record_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _new_card(client):
    response = client.post("/prints", json={"name": "Synthetic attachment lifetime",
                                          "notes": "Preserve operator notes",
                                          "metadata_json": {"build_origin_z_mm": 0.0}})
    assert response.status_code == 200, response.text
    return response.json()["record_id"]


def _upload(client, record_id, name, payload, kind, *, status=200):
    response = client.post(f"/prints/{record_id}/files",
        files={"file": (name, payload, "application/octet-stream")}, data={"file_type": kind})
    assert response.status_code == status, response.text
    return response.json()


def _mesh_payload(width=1):
    cube = trimesh.creation.box(extents=[width, 1, 1])
    cube.apply_translation([0, 0, .5])
    return cube.export(file_type="stl")


def _jobs(record_id):
    with SessionLocal() as db:
        return [{"job_id": row.job_id, "status": row.status, "owner": row.owner_node_id,
                 "payload": dict(row.payload_json), "generation": row.lease_generation,
                 "lease_owner": row.lease_owner, "finished_at": row.finished_at}
                for row in db.scalars(select(BackgroundJob).where(
                    BackgroundJob.job_type == "print_estimate", BackgroundJob.entity_id == record_id,
                ).order_by(BackgroundJob.created_at, BackgroundJob.job_id))]


def _finish(client, record_id, *, parts):
    for _ in range(8):
        if not process_next_estimate("lifetime-native-consumer"):
            break
    else:
        pytest.fail("an unchanged accepted request must not recompute forever")
    current = _card(client, record_id)
    assert current["metadata_json"]["prediction"]["n_parts"] == parts
    assert current["prediction_is_current"] is True
    assert current["prediction_configuration_is_current"] is True
    assert current["estimate_job"] is None
    return current


@pytest.mark.parametrize("workflow", ["pending", "completed", "running"])
def test_old_delete_cannot_remove_restored_geometry_and_native_estimate_finishes(lifetime_client, workflow):
    client = lifetime_client
    record_id = _new_card(client)
    outcome = client.post(f"/prints/{record_id}/quality-outcomes",
        json={"result": "accepted", "inspection_result": "Synthetic fixture, not real inspection"})
    assert outcome.status_code == 200, outcome.text
    quality = client.get(f"/prints/{record_id}/quality-outcomes").json()
    payload = _mesh_payload()
    original = _upload(client, record_id, "a.stl", payload, "stl")
    other = _upload(client, record_id, "b.stl", _mesh_payload(2), "stl")
    doc = _upload(client, record_id, "operator.txt", b"Preserve documentation", "doc")
    if workflow != "pending":
        _finish(client, record_id, parts=2)
    if workflow == "running":
        response = client.post(f"/prints/{record_id}/estimate")
        assert response.status_code == 200, response.text

    restored = {}
    old_url = f"/prints/{record_id}/files/{original['file_id']}"

    def remove_and_restore():
        deletion = client.delete(old_url)
        assert deletion.status_code == 200, deletion.text
        replacement = _upload(client, record_id, "restored-a.stl", payload, "stl")
        assert replacement["object_uri"] != original["object_uri"]
        restored.update(replacement)
        before, jobs, objects = _card(client, record_id), _jobs(record_id), dict(MemoryStore.objects)
        stale = client.delete(old_url)
        assert stale.status_code == 404, stale.text
        assert replacement["file_id"] != original["file_id"]
        assert _card(client, record_id) == before
        assert _jobs(record_id) == jobs and MemoryStore.objects == objects
        duplicate = _upload(client, record_id, "renamed-a.stl", payload, "stl")
        assert duplicate["duplicate"] is True and duplicate["file_id"] == replacement["file_id"]
        assert _card(client, record_id) == before
        assert _jobs(record_id) == jobs and MemoryStore.objects == objects

    if workflow == "running":
        MemoryStore.on_download = remove_and_restore
        assert process_next_estimate("lifetime-running-consumer")
        assert restored, "the replacement happened during the native running calculation"
    else:
        remove_and_restore()
    current = _finish(client, record_id, parts=2)
    assert current["notes"] == "Preserve operator notes"
    owner = get_settings().compute_node_id
    assert current["origin_compute_node_id"] == owner
    assert {job["owner"] for job in _jobs(record_id)} == {owner}
    assert client.get(f"/prints/{record_id}/quality-outcomes").json() == quality
    assert {item["file_id"] for item in current["files"]} == {restored["file_id"], other["file_id"], doc["file_id"]}
    bucket, name = restored["object_uri"].removeprefix("s3://").split("/", 1)
    assert MemoryStore.objects[bucket, name] == payload


@pytest.mark.parametrize("commit_mode", ["failed_commit", "lost_ack"])
def test_failed_or_uncertain_publication_recovers_the_same_durable_id(lifetime_client, monkeypatch, commit_mode):
    client = lifetime_client
    record_id = _new_card(client)
    original_commit = Session.commit
    calls = 0

    def uncertain_commit(db):
        nonlocal calls
        calls += 1
        if calls != 1:
            return original_commit(db)
        if commit_mode == "lost_ack":
            original_commit(db)
        raise OperationalError("COMMIT", None, RuntimeError("synthetic commit failure"),
                               connection_invalidated=commit_mode == "lost_ack")

    with monkeypatch.context() as injected:
        injected.setattr(Session, "commit", uncertain_commit)
        if commit_mode == "failed_commit":
            with pytest.raises(OperationalError):
                _upload(client, record_id, "part.stl", _mesh_payload(), "stl")
        else:
            _upload(client, record_id, "part.stl", _mesh_payload(), "stl", status=202)

    queue = LocalNasOutbox.from_settings(get_settings())
    state = "failed" if commit_mode == "failed_commit" else "pending"
    path = next((queue.root / state).glob("*/manifest.json"))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    operation_id, file_id = manifest["operation_id"], manifest["file_id"]
    before, jobs = _card(client, record_id), _jobs(record_id)
    if commit_mode == "failed_commit":
        assert before["files"] == [] and jobs == []
        assert queue.retry_failed(operation_id)
    else:
        assert before["files"][0]["file_id"] == file_id and len(jobs) == 1
    future = time.time() + 301
    with monkeypatch.context() as controlled:
        controlled.setattr("storage.sync.local_outbox.time", SimpleNamespace(time=lambda: future))
        recovered = process_next(outbox=queue, store=MemoryStore(), operation_id=operation_id)
    assert recovered["status"] == "completed" and recovered["result"]["file_id"] == file_id
    assert _manifest(queue, operation_id)["file_id"] == file_id
    assert _card(client, record_id)["files"][0]["file_id"] == file_id
    assert MemoryStore.puts == (2 if commit_mode == "failed_commit" else 1)
    if commit_mode == "lost_ack":
        assert recovered["result"]["duplicate"] is True
        assert _card(client, record_id) == before and _jobs(record_id) == jobs
    _finish(client, record_id, parts=1)
