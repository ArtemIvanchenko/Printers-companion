"""Accepted estimates continue through ordinary card/import changes, fenced."""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
import trimesh

from api.main import app
from core.config.settings import get_settings
from domain.models.events import LayerSnapshot
from domain.models.jobs import BackgroundJob
from domain.models.prints import PrintRecord, PrintRecordFile
from domain.models.sessions import BuildSession, ImportJob, ReportArtifact
from storage.db.session import SessionLocal, engine
from storage.repositories.jobs_repo import JobsRepository
from worker.estimate_tasks import process_next_estimate
from worker.tasks import process_due_import_jobs


class MemoryStore:
    """Only external object transport is substituted; parsing/math stay native."""
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

    def put_bytes_verified(self, bucket, name, payload, **kwargs):
        self.objects[bucket, name] = payload
        return f"s3://{bucket}/{name}"

    def get_bytes(self, bucket, name):
        return self.objects[bucket, name]

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
def accepted(tmp_path, monkeypatch):
    MemoryStore.objects = {}
    MemoryStore.on_download = None
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", MemoryStore)
    monkeypatch.setattr("api.routes.prints.ObjectStore", MemoryStore)
    monkeypatch.setattr("domain.services.estimation.calculation.ObjectStore", MemoryStore)
    settings = get_settings()
    raw = tmp_path / "raw"
    raw.mkdir()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(raw))
    monkeypatch.setattr(settings, "file_stability_seconds", 0)
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    client = TestClient(app)
    response = client.put("/settings/machine", json={
        "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500, "hatch_distance_mm": .1,
        "layer_thickness_mm": .5, "laser_count": 1, "recoat_time_ms": 9000,
    })
    assert response.status_code == 200, response.text
    response = client.post("/prints", json={"name": "Accepted request continuity",
                                           "metadata_json": {"build_origin_z_mm": 0.0}})
    assert response.status_code == 200, response.text
    record_id = response.json()["record_id"]
    cube = trimesh.creation.box(extents=[1, 1, 1])
    cube.apply_translation([0, 0, .5])
    response = client.post(f"/prints/{record_id}/files",
        files={"file": ("part.stl", cube.export(file_type="stl"), "application/octet-stream")},
        data={"file_type": "stl"})
    assert response.status_code == 200, response.text
    yield client, record_id, response.json()["file_id"]
    MemoryStore.on_download = None
    client.close()


def _jobs(record_id):
    with SessionLocal() as db:
        return [{"job_id": row.job_id, "status": row.status, "owner": row.owner_node_id,
                 "payload": dict(row.payload_json), "error": row.error, "lease_owner": row.lease_owner,
                 "generation": row.lease_generation, "finished_at": row.finished_at}
                for row in db.scalars(select(BackgroundJob).where(
                    BackgroundJob.job_type == "print_estimate", BackgroundJob.entity_id == record_id,
                ).order_by(BackgroundJob.created_at, BackgroundJob.job_id))]


def _edit(client, record_id, **updates):
    current = client.get(f"/prints/{record_id}").json()
    response = client.patch(f"/prints/{record_id}", json={"expected_revision": current["revision"], **updates})
    assert response.status_code == 200, response.text
    return response.json()


def _finish(client, record_id):
    for _ in range(4):
        if not process_next_estimate("continuity-consumer"):
            break
    else:
        pytest.fail("an unchanged accepted request must not recompute forever")
    current = client.get(f"/prints/{record_id}").json()
    prediction = current["metadata_json"]["prediction"]
    assert prediction["input_revision"] == current["revision"] - 1
    assert prediction["computed_by"] == get_settings().compute_node_id
    assert prediction["n_parts"] == 1
    assert current["prediction_is_current"] is True
    assert current["prediction_configuration_is_current"] is True
    assert current["estimate_job"] is None
    return current


def test_stl_then_confirmed_logs_and_final_link_finish_without_manual_reestimate(accepted):
    client, record_id, _ = accepted
    original = _jobs(record_id)[0]
    source = b"OLD_STATS: 1|9000|30000|39300|\nOLD_STATS: 2|9000|31000|40300|\n"
    response = client.post(f"/prints/{record_id}/import-logs",
        files=[("files", ("01.04.2026_time.log", source, "text/plain"))])
    assert response.status_code == 200, response.text
    import_id = response.json()["jobs"][0]["import_job_id"]
    assert process_due_import_jobs("unconfirmed-consumer") == 0
    response = client.post(f"/imports/{import_id}/confirm")
    assert response.status_code == 200, response.text
    assert process_due_import_jobs("continuity-import-consumer") == 1
    linked = client.get(f"/prints/{record_id}").json()
    assert linked["revision"] == original["payload"]["record_revision"] + 2
    with SessionLocal() as db:
        imported = db.get(ImportJob, import_id)
        assert imported.status == "done", imported.error
        assert imported.owner_node_id == original["owner"]
        assert len(imported.session_ids) == len(imported.report_ids) == 1
        session = db.get(BuildSession, imported.session_ids[0])
        assert session.session_id == linked["session_id"]
        manifest = session.context["timing_publication"]
        assert manifest["source"] == {"import_job_id": import_id, "owner_node_id": original["owner"],
                                     "lease_generation": imported.lease_generation}
        assert len(list(db.scalars(select(LayerSnapshot)))) == manifest["row_count"] == 2
        report = db.get(ReportArtifact, imported.report_ids[0])
        assert report.payload["timing_publication"] == manifest
        assert report.storage_uri and imported.source_objects
        raw = Path(imported.source_path) / "01.04.2026_time.log"
    assert raw.read_bytes() == source
    _finish(client, record_id)
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "done"]
    assert jobs[0]["payload"] == original["payload"]
    assert {job["owner"] for job in jobs} == {original["owner"]}
    assert raw.read_bytes() == source


def test_initial_revision_change_continues_one_accepted_request(accepted):
    client, record_id, _ = accepted
    original = _jobs(record_id)[0]
    _edit(client, record_id, notes="Operator context added before calculation")
    assert process_next_estimate("continuity-consumer")
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "pending"]
    assert jobs[0]["payload"] == original["payload"]
    assert jobs[1]["payload"]["record_revision"] > original["payload"]["record_revision"]
    assert jobs[1]["payload"]["manual_rerun"] is False
    current = client.get(f"/prints/{record_id}").json()
    assert current["estimate_job"]["job_id"] == jobs[1]["job_id"]
    assert current["metadata_json"].get("prediction") is None
    _finish(client, record_id)


@pytest.mark.parametrize("change", ["card", "configuration", "competing_request"])
def test_running_input_change_blocks_old_result_and_continues_or_joins(accepted, change):
    client, record_id, _ = accepted
    original = _jobs(record_id)[0]
    seen = {}
    def during_calculation():
        seen["running"] = _jobs(record_id)[0]
        assert seen["running"]["status"] == "running"
        if change == "configuration":
            response = client.put("/settings/machine", json={"recoat_time_ms": 11000})
            assert response.status_code == 200, response.text
        else:
            _edit(client, record_id, notes="Operator context added while computing")
        if change == "competing_request":
            response = client.post(f"/prints/{record_id}/estimate", json={})
            assert response.status_code == 200, response.text
            seen["competitor"] = response.json()["job_id"]
            assert _jobs(record_id)[0] == seen["running"]
    MemoryStore.on_download = during_calculation
    assert process_next_estimate("continuity-consumer")
    jobs = _jobs(record_id)
    assert [job["status"] for job in jobs] == ["failed", "pending"]
    assert jobs[0]["payload"] == original["payload"]
    if change == "configuration":
        assert jobs[0]["payload"]["record_revision"] == jobs[1]["payload"]["record_revision"]
    if change == "competing_request":
        assert jobs[1]["job_id"] == seen["competitor"]
    assert client.get(f"/prints/{record_id}").json()["metadata_json"].get("prediction") is None
    _finish(client, record_id)


def test_foreign_consumer_cannot_claim_or_continue(accepted):
    _, record_id, _ = accepted
    original = _jobs(record_id)
    assert not process_next_estimate("foreign-consumer", owner_node_id="foreign-node")
    assert _jobs(record_id) == original


@pytest.mark.parametrize("change", ["geometry_deleted", "card_deleted", "missing_params", "invalid_strategy", "foreign_card"])
def test_ineligible_current_inputs_do_not_create_a_successor(accepted, change):
    client, record_id, file_id = accepted
    if change == "geometry_deleted":
        response = client.delete(f"/prints/{record_id}/files/{file_id}")
        assert response.status_code == 200, response.text
    elif change == "card_deleted":
        response = client.delete(f"/prints/{record_id}")
        assert response.status_code == 200, response.text
    else:
        _edit(client, record_id, notes="Current inputs are no longer eligible")
        if change == "missing_params":
            response = client.put("/settings/machine", json={"hatch_speed_mm_s": None})
            assert response.status_code == 200, response.text
        elif change == "invalid_strategy":
            # The HTTP contract rejects this input; an adversarial persisted
            # row still cannot authorise a new calculation through recovery.
            with SessionLocal() as db:
                record = db.get(PrintRecord, record_id)
                record.metadata_json = {**record.metadata_json, "scan_strategy": {"hatch_speed_mm_s": -1}}
                db.commit()
        else:
            # An adversarial shared row must not transfer the accepted authority.
            with SessionLocal() as db:
                db.get(PrintRecord, record_id).origin_compute_node_id = "foreign-node"
                db.commit()
    assert process_next_estimate("continuity-consumer")
    jobs = _jobs(record_id)
    assert len(jobs) == 1 and jobs[0]["status"] == "failed"
    assert not process_next_estimate("continuity-consumer")


@pytest.mark.parametrize("change", ["expired", "reclaimed"])
def test_lost_running_lease_cannot_publish_or_invent_a_successor(accepted, change):
    client, record_id, _ = accepted
    def during_calculation():
        _edit(client, record_id, notes="Card changed before the old lease was lost")
        job_id = _jobs(record_id)[0]["job_id"]
        with SessionLocal() as db:
            db.get(BackgroundJob, job_id).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        if change == "reclaimed":
            with SessionLocal() as db:
                replacement = JobsRepository(db).claim_next("print_estimate", owner_node_id=get_settings().compute_node_id,
                                                            lease_owner="replacement-consumer")
                db.commit()
            assert replacement["job_id"] == job_id and replacement["lease_generation"] == 2
    MemoryStore.on_download = during_calculation
    assert process_next_estimate("continuity-consumer")
    jobs = _jobs(record_id)
    assert len(jobs) == 1 and jobs[0]["status"] == "running"
    assert jobs[0]["generation"] == (2 if change == "reclaimed" else 1)
    assert jobs[0]["lease_owner"] == ("replacement-consumer" if change == "reclaimed" else "continuity-consumer")
    assert client.get(f"/prints/{record_id}").json()["metadata_json"].get("prediction") is None


def test_continuation_rechecks_live_lease_after_configuration_wait(accepted):
    client, record_id, _ = accepted
    _edit(client, record_id, notes="Current revision differs")
    expired = []
    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE" and not expired:
            expired.append(True)
            with SessionLocal() as db:
                db.get(BackgroundJob, _jobs(record_id)[0]["job_id"]).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
                db.commit()
    event.listen(engine, "before_cursor_execute", before_sql)
    try:
        assert process_next_estimate("continuity-consumer")
    finally:
        event.remove(engine, "before_cursor_execute", before_sql)
    assert expired
    jobs = _jobs(record_id)
    assert len(jobs) == 1 and jobs[0]["status"] == "running" and jobs[0]["error"] is None


def test_expiry_between_strict_check_and_fenced_retirement_creates_no_request(accepted):
    client, record_id, _ = accepted
    _edit(client, record_id, notes="Current revision differs")
    fence_reads = []
    started = []
    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE" and not started:
            started.append(conn)
            with SessionLocal() as db:
                db.get(BackgroundJob, _jobs(record_id)[0]["job_id"]).lease_until = datetime.now(timezone.utc) + timedelta(seconds=1)
                db.commit()
        elif (started and conn is started[0] and statement.startswith("SELECT background_jobs.job_id,")
              and "WHERE background_jobs.job_id =" in statement):
            fence_reads.append(statement)
            if len(fence_reads) == 2:
                time.sleep(1.1)  # Genuine TTL expiry while the job row remains locked.
    event.listen(engine, "before_cursor_execute", before_sql)
    try:
        assert process_next_estimate("continuity-consumer")
    finally:
        event.remove(engine, "before_cursor_execute", before_sql)
    assert len(fence_reads) >= 2
    jobs = _jobs(record_id)
    assert len(jobs) == 1 and jobs[0]["status"] == "running" and jobs[0]["error"] is None


def test_enqueue_failure_rolls_back_old_retirement_and_recovery_is_fenced(accepted):
    client, record_id, _ = accepted
    original = _jobs(record_id)[0]
    _edit(client, record_id, notes="Current revision differs")
    with engine.begin() as conn:
        conn.exec_driver_sql("""CREATE TRIGGER refuse_estimate_continuation BEFORE INSERT ON background_jobs
            WHEN NEW.job_type = 'print_estimate' BEGIN SELECT RAISE(ABORT, 'synthetic enqueue failure'); END""")
    try:
        with pytest.raises(IntegrityError, match="synthetic enqueue failure"):
            process_next_estimate("continuity-consumer")
        jobs = _jobs(record_id)
        assert len(jobs) == 1 and jobs[0]["status"] == "running"
        assert jobs[0]["payload"] == original["payload"]
        assert jobs[0]["error"] is None and jobs[0]["finished_at"] is None
        assert jobs[0]["lease_owner"] == "continuity-consumer" and jobs[0]["generation"] == 1
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TRIGGER refuse_estimate_continuation")
    with SessionLocal() as db:
        db.get(BackgroundJob, original["job_id"]).lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    _finish(client, record_id)
    assert [job["status"] for job in _jobs(record_id)] == ["failed", "done"]
    assert _jobs(record_id)[0]["generation"] == 2


@pytest.mark.parametrize("revision", [None, 0, True])
def test_invalid_request_revision_is_terminal_not_new_compute_permission(accepted, revision):
    _, record_id, _ = accepted
    with SessionLocal() as db:
        job = db.get(BackgroundJob, _jobs(record_id)[0]["job_id"])
        job.payload_json = {**job.payload_json, "record_revision": revision}
        db.commit()
    assert process_next_estimate("continuity-consumer")
    assert len(_jobs(record_id)) == 1 and _jobs(record_id)[0]["status"] == "failed"
    assert not process_next_estimate("continuity-consumer")


@pytest.mark.parametrize("owner", [None, "foreign-node"])
def test_invalid_payload_owner_cannot_continue_or_reclaim_forever(accepted, owner):
    _, record_id, _ = accepted
    with SessionLocal() as db:
        job = db.get(BackgroundJob, _jobs(record_id)[0]["job_id"])
        job.payload_json = {**job.payload_json, "owner_node_id": owner}
        db.commit()
    assert process_next_estimate("continuity-consumer")
    assert len(_jobs(record_id)) == 1 and _jobs(record_id)[0]["status"] == "failed"
    assert not process_next_estimate("continuity-consumer")


@pytest.mark.parametrize("field", ["input_hash", "input_revision"])
def test_corrupted_native_snapshot_with_unchanged_inputs_is_not_requeued(accepted, monkeypatch, field):
    from domain.services.estimation import calculation

    _, record_id, _ = accepted
    native = calculation.calculate_prediction_snapshot
    def corrupted(*args, **kwargs):
        snapshot = native(*args, **kwargs)
        snapshot[field] = "corrupted" if field == "input_hash" else snapshot[field] + 1
        return snapshot
    monkeypatch.setattr(calculation, "calculate_prediction_snapshot", corrupted)
    assert process_next_estimate("continuity-consumer")
    assert len(_jobs(record_id)) == 1 and _jobs(record_id)[0]["status"] == "failed"
    assert not process_next_estimate("continuity-consumer")


def test_completed_workflow_is_not_restarted_by_a_later_context_edit(accepted):
    client, record_id, _ = accepted
    historical = _finish(client, record_id)["metadata_json"]["prediction"]
    completed = _jobs(record_id)
    _edit(client, record_id, notes="Context changed after the accepted calculation finished")
    assert not process_next_estimate("continuity-consumer")
    assert _jobs(record_id) == completed
    assert client.get(f"/prints/{record_id}").json()["metadata_json"]["prediction"] == historical


def test_uri_only_change_does_not_claim_a_terminal_same_key_job_as_successor(accepted, caplog):
    client, record_id, file_id = accepted
    original = _jobs(record_id)[0]
    def during_calculation():
        with SessionLocal() as db:
            file = db.get(PrintRecordFile, file_id)
            bucket, _, old_name = file.object_uri.removeprefix("s3://").partition("/")
            fresh_name = "synthetic-new-uri/" + old_name
            MemoryStore.objects[bucket, fresh_name] = MemoryStore.objects[bucket, old_name]
            file.object_uri = f"s3://{bucket}/{fresh_name}"
            db.commit()
    MemoryStore.on_download = during_calculation
    assert process_next_estimate("continuity-consumer")
    jobs = _jobs(record_id)
    # Existing request identity omits URI; this is not permission to invent a
    # force/manual key or to call the failed original an active successor.
    assert len(jobs) == 1 and jobs[0]["job_id"] == original["job_id"] and jobs[0]["status"] == "failed"
    assert "no active successor" in caplog.text
    assert not process_next_estimate("continuity-consumer")
    assert client.get(f"/prints/{record_id}").json()["metadata_json"].get("prediction") is None
