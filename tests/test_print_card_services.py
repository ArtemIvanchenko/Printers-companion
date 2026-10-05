"""Card/attachment application boundaries, including failures between stores."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app
from storage.db.session import engine, session_scope
from storage.repositories.prints_repo import PrintsRepository


class MemoryStore:
    objects = {}
    on_put = None
    on_stream = None
    on_remove = None

    def __init__(self, *args, **kwargs):
        pass

    def is_available(self):
        return True

    def put_file(self, bucket, name, path, **kwargs):
        self.objects[bucket, name] = Path(path).read_bytes()
        if type(self).on_put:
            type(self).on_put()
        return f"s3://{bucket}/{name}"

    def remove_object(self, bucket, name):
        if type(self).on_remove:
            type(self).on_remove()
        return self.objects.pop((bucket, name), None) is not None

    def open_stream(self, bucket, name):
        if type(self).on_stream:
            type(self).on_stream()
        data = self.objects.get((bucket, name))
        return iter([data]) if data is not None else None


@pytest.fixture
def card_client(monkeypatch):
    MemoryStore.objects = {}
    MemoryStore.on_put = MemoryStore.on_stream = MemoryStore.on_remove = None
    monkeypatch.setattr("api.routes.prints.ObjectStore", MemoryStore)
    yield TestClient(app)
    MemoryStore.on_put = MemoryStore.on_stream = MemoryStore.on_remove = None


def new_card(client):
    response = client.post("/prints", json={"name": "Карточка без даты"})
    assert response.status_code == 200
    return response.json()


def upload(client, record, name="drawing.pdf"):
    return client.post(
        f"/prints/{record['record_id']}/files",
        files={"file": (name, b"durable attachment", "application/pdf")},
        data={"file_type": "doc"},
    )


def test_upload_does_not_replace_date_edited_during_transfer(card_client):
    record = new_card(card_client)
    chosen = datetime(2026, 8, 15, tzinfo=timezone.utc)

    def edit():
        with session_scope() as db:
            PrintsRepository(db).update_print_record(record["record_id"], {"printed_at": chosen})

    MemoryStore.on_put = edit
    response = upload(card_client, record, "01.08.2026_drawing.pdf")
    assert response.status_code == 200, response.text
    current = card_client.get(f"/prints/{record['record_id']}").json()
    assert current["printed_at"].startswith("2026-08-15")


@pytest.mark.parametrize("whole_card", [True, False])
def test_delete_commit_failure_preserves_remote_bytes(card_client, monkeypatch, whole_card):
    record = new_card(card_client)
    attached = upload(card_client, record).json()
    original = dict(MemoryStore.objects)

    def fail_commit(self):
        raise OperationalError("COMMIT", {}, ConnectionError("database unavailable"))

    path = f"/prints/{record['record_id']}"
    if not whole_card:
        path += f"/files/{attached['file_id']}"
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(OperationalError):
            card_client.delete(path)
    assert MemoryStore.objects == original
    with session_scope() as db:
        assert PrintsRepository(db).get_print_record(record["record_id"])
        assert len(PrintsRepository(db).list_print_files(record["record_id"])) == 1


def test_download_releases_sql_before_opening_object_stream(card_client):
    record = new_card(card_client)
    attached = upload(card_client, record).json()
    held = set()

    def checkout(connection, record, proxy):
        held.add(id(connection))

    def checkin(connection, record):
        held.discard(id(connection))

    def check_stream():
        assert not held, "download kept a NAS SQL connection while opening MinIO"

    MemoryStore.on_stream = check_stream
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        response = card_client.get(
            f"/prints/{record['record_id']}/files/{attached['file_id']}/download"
        )
        assert response.content == b"durable attachment"
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


@pytest.mark.parametrize("whole_card", [True, False])
def test_cleanup_retains_objects_referenced_by_another_card(card_client, whole_card):
    first, second = new_card(card_client), new_card(card_client)
    original = upload(card_client, first).json()
    with session_scope() as db:
        shared = PrintsRepository(db).add_print_file(
            {
                **{
                    key: original[key]
                    for key in (
                        "object_uri",
                        "file_name",
                        "file_type",
                        "size_bytes",
                        "checksum",
                    )
                },
                "record_id": second["record_id"],
            }
        )
    path = f"/prints/{first['record_id']}"
    if not whole_card:
        path += f"/files/{original['file_id']}"
    assert card_client.delete(path).status_code == 200
    assert len(MemoryStore.objects) == 1
    response = card_client.get(f"/prints/{second['record_id']}/files/{shared['file_id']}/download")
    assert response.status_code == 200 and response.content == b"durable attachment"


def test_cleanup_only_observes_committed_deletion(card_client):
    record = new_card(card_client)
    attached = upload(card_client, record).json()
    removals = []

    def check_commit():
        with session_scope() as db:
            assert PrintsRepository(db).list_print_files(record["record_id"]) == []
        removals.append(True)

    MemoryStore.on_remove = check_commit
    assert (
        card_client.delete(f"/prints/{record['record_id']}/files/{attached['file_id']}").status_code
        == 200
    )
    assert removals == [True] and MemoryStore.objects == {}


def test_upload_has_no_sql_connection_during_object_transfer(card_client):
    record = new_card(card_client)
    held, checked = set(), []

    def checkout(connection, record, proxy):
        held.add(id(connection))

    def checkin(connection, record):
        held.discard(id(connection))

    def check_put():
        assert not held
        checked.append(True)

    MemoryStore.on_put = check_put
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        response = upload(card_client, record)
        assert response.status_code == 200 and checked == [True]
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_card_deleted_during_transfer_cannot_receive_a_file(card_client):
    record = new_card(card_client)

    def delete():
        with session_scope() as db:
            PrintsRepository(db).delete_print_record(record["record_id"])

    MemoryStore.on_put = delete
    assert upload(card_client, record).status_code == 404
    with session_scope() as db:
        assert PrintsRepository(db).list_print_files(record["record_id"]) == []
    assert MemoryStore.objects == {}


def test_changed_owner_cannot_receive_old_geometry_upload(card_client):
    from domain.models.prints import PrintRecord

    record = new_card(card_client)

    def change_owner():
        with session_scope() as db:
            db.get(PrintRecord, record["record_id"]).origin_compute_node_id = "another-pc"

    MemoryStore.on_put = change_owner
    response = card_client.post(
        f"/prints/{record['record_id']}/files",
        files={"file": ("body.stl", b"geometry", "model/stl")},
        data={"file_type": "stl"},
    )
    assert response.status_code == 403
    with session_scope() as db:
        assert PrintsRepository(db).list_print_files(record["record_id"]) == []
    assert MemoryStore.objects == {}


def test_card_geometry_mapping_uses_detached_inputs(card_client, monkeypatch):
    from domain.models.sessions import BuildSession

    record = new_card(card_client)
    with session_scope() as db:
        db.add(
            BuildSession(
                session_id="card_geometry",
                origin_compute_node_id=record["origin_compute_node_id"],
                context={},
            )
        )
        db.flush()
        PrintsRepository(db).update_print_record(
            record["record_id"],
            {
                "session_id": "card_geometry",
                "metadata_json": {"prediction": {"scan_geometry": {"layers": []}}},
            },
        )
    held, checked = set(), []

    def checkout(connection, record, proxy):
        held.add(id(connection))

    def checkin(connection, record):
        held.discard(id(connection))

    def map_geometry(*args, **kwargs):
        assert not held
        checked.append(True)
        return {"status": "ok", "items": []}

    monkeypatch.setattr("analytics.geometry_context.map_anomalies_to_geometry", map_geometry)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        result = card_client.get(f"/prints/{record['record_id']}")
        assert result.status_code == 200 and checked == [True]
        assert result.json()["geometry_analysis"]["status"] == "ok"
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_card_reads_stored_anomalies_with_real_geometry_mapper(card_client):
    from domain.models.jobs import BackgroundJob
    from domain.models.sessions import BuildSession

    record = new_card(card_client)
    snapshot = {
        "zs": [0.0, 10.0], "z_min": 0.0, "z_max": 10.0,
        "hatch_mm": [100.0, 100.0], "contour_mm": [40.0, 40.0],
        "jump_mm": [10.0, 10.0], "n_jumps": [2.0, 2.0], "open_mm": [0.0, 0.0],
        "layer_thickness_mm": 0.1, "layer_count": 100,
    }
    with session_scope() as db:
        db.add(BuildSession(
            session_id="card_geometry_projection",
            origin_compute_node_id=record["origin_compute_node_id"],
            context={"runtime_payload": {"group": {
                "health": {
                    "burn_drift": {"outlier_layers": [{"layer": 21}]},
                    "anomalies": [{"signal": "SO1", "kind": "spike", "sample_index": 5}],
                },
                "telemetry": {
                    "time": list(range(11)),
                    "layer_burn_times": [{"layer": layer} for layer in range(1, 101)],
                },
                "unused_analysis": {"detail": "not needed for mapping" * 1000},
            }, "files": [{"unused_source_metadata": "source" * 1000}]}},
        ))
        db.add(BackgroundJob(
            job_id="card_projection_estimate", job_type="print_estimate",
            owner_node_id=record["origin_compute_node_id"], entity_type="print_record",
            entity_id=record["record_id"], idempotency_key="card_projection_estimate",
            payload_json={"unused_estimation_input": "large input" * 1000},
        ))
        db.flush()
        PrintsRepository(db).update_print_record(record["record_id"], {
            "session_id": "card_geometry_projection",
            "metadata_json": {"prediction": {
                "scan_geometry": snapshot,
                "geometry_quality": {"status": "lower_bound"},
            }},
        })

    materialized = []

    def loaded(session, instance):
        if isinstance(instance, (BuildSession, BackgroundJob)):
            materialized.append(type(instance).__name__)

    event.listen(Session, "loaded_as_persistent", loaded)
    try:
        response = card_client.get(f"/prints/{record['record_id']}")
    finally:
        event.remove(Session, "loaded_as_persistent", loaded)

    assert response.status_code == 200
    assert materialized == ["BuildSession"]
    assert response.json()["estimate_job"] == {
        "job_id": "card_projection_estimate", "status": "pending",
    }
    result = response.json()["geometry_analysis"]
    assert result["status"] == "ok"
    assert [(item["layer"], item["mapping_precision"]) for item in result["items"]] == [
        (21, "exact_layer"), (51, "approximate_progress"),
    ]
    assert result["geometry_confidence"]["level"] == "low"
    assert result["geometry_confidence"]["build_origin_confirmed"] is False
    assert all(item["geometry"]["relative_path_length_pct"] == 100.0
               for item in result["items"])


def test_sync_queues_geometry_estimate_for_its_explicit_owner(card_client, tmp_path):
    import hashlib
    from core.config.settings import get_settings
    from domain.models.jobs import BackgroundJob
    from storage.sync.local_outbox import LocalNasOutbox
    from worker.nas_sync import process_next

    settings = get_settings().model_copy(update={"compute_node_id": "operator-01"})
    with session_scope() as db:
        record = PrintsRepository(db).create_print_record(
            {"name": "local owner", "origin_compute_node_id": "operator-01"}
        )
    # Use the existing fixture file as bytes; enqueue_attachment owns durable staging.
    source = Path(__file__)
    content = source.read_bytes()
    queue = LocalNasOutbox.from_settings(settings)
    queue.enqueue_attachment(
        source,
        owner_node_id="operator-01",
        record_id=record["record_id"],
        file_name="body.stl",
        file_type="stl",
        bucket="stls",
        checksum=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
    )
    state = process_next(outbox=queue, store=MemoryStore(), settings=settings)
    assert state["status"] == "completed", state
    with session_scope() as db:
        jobs = db.scalars(
            select(BackgroundJob).where(BackgroundJob.job_type == "print_estimate")
        ).all()
        assert len(jobs) == 1 and jobs[0].owner_node_id == "operator-01"
        assert jobs[0].payload_json["owner_node_id"] == "operator-01"


def test_card_error_survives_serialization():
    import pickle
    from domain.services.print_cards.contracts import CardError

    restored = pickle.loads(pickle.dumps(CardError("conflict", {"message": "другая версия"})))
    assert restored.code == "conflict" and restored.detail == {"message": "другая версия"}
