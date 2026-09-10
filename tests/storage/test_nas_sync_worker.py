import hashlib
from pathlib import Path

from core.config.settings import get_settings
from storage.db.session import SessionLocal
from storage.repositories.prints_repo import PrintsRepository
from storage.sync.local_outbox import LocalNasOutbox
from worker.nas_sync import process_next


class _Store:
    def __init__(self, available=True):
        self.available = available
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts = 0

    def is_available(self):
        return self.available

    def put_file(self, bucket, object_name, path, content_type="application/octet-stream"):
        self.puts += 1
        self.objects[(bucket, object_name)] = Path(path).read_bytes()
        return f"s3://{bucket}/{object_name}"

    def remove_object(self, bucket, object_name):
        return self.objects.pop((bucket, object_name), None) is not None


def _settings(tmp_path, node="operator-01"):
    return get_settings().model_copy(update={
        "app_env": "test",
        "compute_node_id": node,
        "nas_outbox_path": str(tmp_path / node),
        "nas_outbox_max_bytes": 10_000,
        "nas_sync_retry_min_seconds": 1,
        "nas_sync_retry_max_seconds": 1,
        "nas_sync_claim_seconds": 60,
    })


def _enqueue(settings, source, record_id, *, file_type="doc"):
    queue = LocalNasOutbox.from_settings(settings)
    data = source.read_bytes()
    operation = queue.enqueue_attachment(
        source,
        owner_node_id=settings.compute_node_id,
        record_id=record_id,
        file_name=source.name,
        file_type=file_type,
        bucket="stls" if file_type in ("stl", "stl_supports") else "docs",
        checksum=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )
    return queue, operation


def test_worker_publishes_object_before_atomic_database_pointer(tmp_path):
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "offline drawing",
            "origin_compute_node_id": "operator-01",
        })
        db.commit()

    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"pdf-data")
    settings = _settings(tmp_path)
    queue, operation = _enqueue(settings, source, record["record_id"])
    store = _Store()

    state = process_next(outbox=queue, store=store, settings=settings)

    assert state["status"] == "completed"
    assert store.puts == 1
    with SessionLocal() as db:
        files = PrintsRepository(db).list_print_files(record["record_id"])
    assert len(files) == 1
    assert files[0]["checksum"] == hashlib.sha256(b"pdf-data").hexdigest()
    assert queue.get(operation["operation_id"])["result"]["file_id"] == files[0]["file_id"]


def test_two_workstations_same_content_are_deduplicated_by_shared_db(tmp_path):
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "shared card",
            "origin_compute_node_id": "operator-01",
        })
        db.commit()

    source = tmp_path / "same.pdf"
    source.write_bytes(b"same-content")
    first_settings = _settings(tmp_path, "operator-01")
    second_settings = _settings(tmp_path, "operator-02")
    first_queue, _ = _enqueue(first_settings, source, record["record_id"])
    second_queue, _ = _enqueue(second_settings, source, record["record_id"])
    store = _Store()

    assert process_next(
        outbox=first_queue, store=store, settings=first_settings,
    )["status"] == "completed"
    second = process_next(outbox=second_queue, store=store, settings=second_settings)

    assert second["status"] == "completed"
    assert second["result"]["duplicate"] is True
    assert store.puts == 1, "second PC skips upload after the DB checksum pre-check"
    with SessionLocal() as db:
        assert len(PrintsRepository(db).list_print_files(record["record_id"])) == 1


def test_nas_outage_keeps_verified_payload_pending_locally(tmp_path):
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "queued card",
            "origin_compute_node_id": "operator-01",
        })
        db.commit()

    source = tmp_path / "queued.pdf"
    source.write_bytes(b"keep-me")
    settings = _settings(tmp_path)
    queue, operation = _enqueue(settings, source, record["record_id"])

    state = process_next(outbox=queue, store=_Store(available=False), settings=settings)

    assert state["status"] == "pending"
    queued_payload = queue.root / "pending" / operation["operation_id"] / "payload"
    assert queued_payload.read_bytes() == b"keep-me"


def test_missing_card_is_quarantined_without_losing_local_payload(tmp_path):
    source = tmp_path / "orphan.pdf"
    source.write_bytes(b"orphan")
    settings = _settings(tmp_path)
    queue, operation = _enqueue(settings, source, "pr_missing")
    store = _Store()

    state = process_next(outbox=queue, store=store, settings=settings)

    assert state["status"] == "failed"
    assert "не существует" in state["last_error"]
    assert store.puts == 0
    payload = queue.root / "failed" / operation["operation_id"] / "payload"
    assert payload.read_bytes() == b"orphan"


def test_foreign_pc_cannot_publish_compute_input(tmp_path):
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "owned plate",
            "origin_compute_node_id": "operator-01",
        })
        db.commit()

    source = tmp_path / "foreign.stl"
    source.write_bytes(b"mesh")
    settings = _settings(tmp_path, "operator-02")
    queue, _ = _enqueue(
        settings,
        source,
        record["record_id"],
        file_type="stl",
    )
    store = _Store()

    state = process_next(outbox=queue, store=store, settings=settings)

    assert state["status"] == "failed"
    assert "belongs to compute node" in state["last_error"]
    assert store.puts == 0
