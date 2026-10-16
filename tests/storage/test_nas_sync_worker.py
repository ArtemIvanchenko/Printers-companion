import hashlib
import io
from pathlib import Path

import pytest
from psycopg.errors import InvalidPassword, TooManyConnections, UndefinedColumn
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from core.config.settings import get_settings
from domain.services.print_cards.attachments import upload_attachment
from storage.db.session import SessionLocal, engine
from storage.repositories.prints_repo import PrintsRepository
from storage.sync.local_outbox import LocalNasOutbox
from worker.nas_sync import process_next


class _Store:
    def __init__(self, available=True, on_put=None):
        self.available = available
        self.objects: dict[tuple[str, str], bytes] = {}
        self.puts = 0
        self.removes = 0
        self.on_put = on_put

    def is_available(self):
        return self.available

    def put_file(self, bucket, object_name, path, content_type="application/octet-stream"):
        self.puts += 1
        self.objects[(bucket, object_name)] = Path(path).read_bytes()
        if self.on_put:
            self.on_put()
        return f"s3://{bucket}/{object_name}"

    def remove_object(self, bucket, object_name):
        self.removes += 1
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
        bucket="stls" if file_type in ("stl", "stl_supports") else "photos" if file_type == "photo" else "docs",
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


@pytest.mark.parametrize("file_type", ["doc", "photo"])
def test_two_workstations_same_content_are_deduplicated_by_shared_db(tmp_path, file_type):
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
    first_queue, _ = _enqueue(first_settings, source, record["record_id"], file_type=file_type)
    second_queue, _ = _enqueue(second_settings, source, record["record_id"], file_type=file_type)
    store = _Store()

    assert process_next(
        outbox=first_queue, store=store, settings=first_settings,
    )["status"] == "completed"
    second = process_next(outbox=second_queue, store=store, settings=second_settings)

    assert second["status"] == "completed"
    assert second["result"]["duplicate"] is True
    assert store.puts == 1, "second PC skips upload after the DB checksum pre-check"
    assert len(store.objects) == 1
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


@pytest.fixture
def sqlite_outer_transactions():
    # SQLite's deferred BEGIN can otherwise let a released INSERT savepoint
    # commit before the outer Session.commit we are deliberately interrupting.
    def begin(connection):
        connection.exec_driver_sql("BEGIN")
    event.listen(engine, "begin", begin)
    try:
        yield
    finally:
        event.remove(engine, "begin", begin)


def _sql_failure(monkeypatch, phase, error, transport):
    if phase == "precheck":
        def fail(self, record_id):
            if error is None:
                self.db.execute(text("SELECT missing_upload_column FROM print_records"))
            raise error
        monkeypatch.setattr(PrintsRepository, "get_print_record", fail)
        monkeypatch.setattr(PrintsRepository, "get_print_record_for_update", fail)
    elif phase == "publication":
        original = PrintsRepository.add_print_file
        def fail(self, values):
            if error is None:
                return original(self, {**values, "file_name": None})
            raise error
        monkeypatch.setattr(PrintsRepository, "add_print_file", fail)
    else:
        original = Session.commit
        calls = 0
        def fail(self):
            nonlocal calls
            calls += 1
            if transport == "worker" and calls == 1:
                return original(self)  # The read-only pre-check commits first.
            raise error
        monkeypatch.setattr(Session, "commit", fail)


SQL_FAILURES = [
    pytest.param("precheck", None, False, id="query_bug"),
    pytest.param("publication", None, False, id="constraint"),
    pytest.param("publication", ProgrammingError("SQL", None, UndefinedColumn("schema mismatch")), False, id="programming"),
    pytest.param("precheck", OperationalError("SQL", None, RuntimeError("disconnect"), connection_invalidated=True), True, id="precheck_disconnect"),
    pytest.param("publication", OperationalError("SQL", None, RuntimeError("disconnect"), connection_invalidated=True), True, id="publication_disconnect"),
    pytest.param("commit", OperationalError("COMMIT", None, RuntimeError("disconnect"), connection_invalidated=True), True, id="commit_disconnect"),
    pytest.param("commit", OperationalError(None, None, TooManyConnections("connection slots full")), True, id="capacity"),
    pytest.param("commit", OperationalError(None, None, InvalidPassword("invalid credentials")), False, id="authentication"),
]


@pytest.mark.parametrize("phase,error,outage", SQL_FAILURES)
@pytest.mark.parametrize("transport", ["http", "worker"])
def test_sql_upload_failures_preserve_payload_and_retry_only_availability(
    tmp_path, monkeypatch, sqlite_outer_transactions, phase, error, outage, transport
):
    settings = _settings(tmp_path)
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "durable SQL failure",
            "origin_compute_node_id": settings.compute_node_id,
        })
        db.commit()
    store = _Store()
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"preserved attachment")
    queue = LocalNasOutbox.from_settings(settings)
    operation_id = None
    if transport == "worker":
        queue, operation = _enqueue(settings, source, record["record_id"])
        operation_id = operation["operation_id"]

    with monkeypatch.context() as patch:
        _sql_failure(patch, phase, error, transport)
        if transport == "worker":
            state = process_next(outbox=queue, store=store, settings=settings)
        else:
            with SessionLocal() as db:
                if outage:
                    result = upload_attachment(
                        PrintsRepository(db), record["record_id"], io.BytesIO(source.read_bytes()),
                        file_name=source.name, file_type="doc", settings=settings,
                        store_factory=lambda: store,
                    )
                    assert result.queued
                    operation_id = result.payload["sync_operation_id"]
                else:
                    with pytest.raises(SQLAlchemyError):
                        upload_attachment(
                            PrintsRepository(db), record["record_id"], io.BytesIO(source.read_bytes()),
                            file_name=source.name, file_type="doc", settings=settings,
                            store_factory=lambda: store,
                        )
                    if phase != "precheck":
                        operation_id = next((queue.root / "failed").iterdir()).name
            state = queue.get(operation_id) if operation_id else None

    with SessionLocal() as db:
        repo = PrintsRepository(db)
        assert repo.list_print_files(record["record_id"]) == []
        assert repo.get_print_record(record["record_id"])["revision"] == record["revision"]
    if transport == "http" and phase == "precheck" and not outage:
        assert queue.status()["bytes_waiting"] == 0
        assert store.puts == 0
        return
    assert state["status"] == ("pending" if outage else "failed")
    assert not list((queue.root / "completed").iterdir())
    payload = queue.root / state["status"] / operation_id / "payload"
    assert payload.read_bytes() == source.read_bytes()
    assert store.puts == (0 if transport == "worker" and phase == "precheck" else 1)
    previous_objects = set(store.objects)
    if outage:
        manifest_path = payload.parent / "manifest.json"
        manifest = queue._read_json(manifest_path)
        manifest["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
        queue._write_json(manifest_path, manifest)
    else:
        assert queue.retry_failed(operation_id)
    restored = process_next(outbox=queue, store=store, settings=settings)
    assert restored["status"] == "completed"
    assert not payload.exists()
    bucket, _, name = restored["result"]["object_uri"].removeprefix("s3://").partition("/")
    assert (bucket, name) not in previous_objects
    assert store.objects[bucket, name] == source.read_bytes()
    assert previous_objects.issubset(store.objects), "failed attempts are retained, never reused"
    with SessionLocal() as db:
        files = PrintsRepository(db).list_print_files(record["record_id"])
        assert len(files) == 1
        assert restored["result"]["file_id"] == files[0]["file_id"]


@pytest.mark.parametrize("transport", ["http", "worker"])
def test_lost_commit_ack_keeps_payload_until_catalogue_is_confirmed(
    tmp_path, monkeypatch, sqlite_outer_transactions, transport
):
    settings = _settings(tmp_path)
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "unknown commit", "origin_compute_node_id": settings.compute_node_id,
        })
        db.commit()
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"confirmed remote bytes")
    queue = LocalNasOutbox.from_settings(settings)
    store = _Store()
    if transport == "worker":
        queue, operation = _enqueue(settings, source, record["record_id"])
        operation_id = operation["operation_id"]
    original = Session.commit
    calls = 0
    def uncertain(self):
        nonlocal calls
        calls += 1
        original(self)
        if transport == "http" or calls == 2:
            raise OperationalError("COMMIT", None, RuntimeError("ack lost"), connection_invalidated=True)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", uncertain)
        if transport == "worker":
            state = process_next(outbox=queue, store=store, settings=settings)
        else:
            with SessionLocal() as db:
                result = upload_attachment(
                    PrintsRepository(db), record["record_id"], io.BytesIO(source.read_bytes()),
                    file_name=source.name, file_type="doc", settings=settings,
                    store_factory=lambda: store,
                )
            assert result.queued
            operation_id = result.payload["sync_operation_id"]
            state = queue.get(operation_id)
    assert state["status"] == "pending"
    payload = queue.root / "pending" / operation_id / "payload"
    assert payload.read_bytes() == source.read_bytes()
    assert not list((queue.root / "completed").iterdir())
    with SessionLocal() as db:
        repo = PrintsRepository(db)
        assert len(repo.list_print_files(record["record_id"])) == 1
        committed_revision = repo.get_print_record(record["record_id"])["revision"]
    manifest_path = payload.parent / "manifest.json"
    manifest = queue._read_json(manifest_path)
    manifest["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
    queue._write_json(manifest_path, manifest)
    restored = process_next(outbox=queue, store=store, settings=settings)
    assert restored["status"] == "completed"
    assert restored["result"]["duplicate"]
    assert store.puts == 1
    assert len(store.objects) == 1
    assert not payload.exists()
    with SessionLocal() as db:
        repo = PrintsRepository(db)
        assert len(repo.list_print_files(record["record_id"])) == 1
        assert repo.get_print_record(record["record_id"])["revision"] == committed_revision


def _replace_claim(queue, operation_id):
    manifest_path = queue.root / "processing" / operation_id / "manifest.json"
    manifest = queue._read_json(manifest_path)
    manifest["claimed_at"] = "2000-01-01T00:00:00+00:00"
    queue._write_json(manifest_path, manifest)
    return queue.claim(operation_id)


@pytest.mark.parametrize("transport", ["http", "worker"])
@pytest.mark.parametrize("phase", ["transfer", "after_commit"])
def test_replaced_publisher_cannot_publish_or_ack_new_claim(
    tmp_path, monkeypatch, sqlite_outer_transactions, transport, phase
):
    settings = _settings(tmp_path)
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "claim race", "origin_compute_node_id": settings.compute_node_id,
        })
        db.commit()
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"immutable race bytes")
    queue = LocalNasOutbox.from_settings(settings)
    store = _Store()
    if transport == "worker":
        queue, _ = _enqueue(settings, source, record["record_id"])
    replacement = None
    def reclaim():
        nonlocal replacement
        operation_id = next((queue.root / "processing").iterdir()).name
        replacement = _replace_claim(queue, operation_id)
    with monkeypatch.context() as patch:
        if phase == "transfer":
            store.on_put = reclaim
        else:
            original = LocalNasOutbox.complete
            def complete(self, operation_id, result, *, claim_token):
                reclaim()
                return original(self, operation_id, result, claim_token=claim_token)
            patch.setattr(LocalNasOutbox, "complete", complete)
        if transport == "worker":
            state = process_next(outbox=queue, store=store, settings=settings)
        else:
            with SessionLocal() as db:
                result = upload_attachment(
                    PrintsRepository(db), record["record_id"], io.BytesIO(source.read_bytes()),
                    file_name=source.name, file_type="doc", settings=settings,
                    store_factory=lambda: store,
                )
            assert result.queued, "stale HTTP claimant must not report its result confirmed"
            state = queue.get(result.payload["sync_operation_id"])
    store.on_put = None
    assert state["status"] == "processing"
    assert queue.status()["counts"]["pending"] == 0
    assert queue.status()["counts"]["failed"] == 0
    assert not list((queue.root / "completed").iterdir())
    payload = Path(replacement["payload_path"])
    assert payload.read_bytes() == source.read_bytes()
    assert queue._read_json(payload.parent / "manifest.json")["claim_token"] == replacement["claim_token"]
    assert store.puts == 1 and store.removes == 0
    with SessionLocal() as db:
        repo = PrintsRepository(db)
        assert len(repo.list_print_files(record["record_id"])) == (0 if phase == "transfer" else 1)
        assert repo.get_print_record(record["record_id"])["revision"] == record["revision"] + (phase == "after_commit")
    assert queue.release(replacement["operation_id"], "next consumer pass", claim_token=replacement["claim_token"])
    manifest_path = queue.root / "pending" / replacement["operation_id"] / "manifest.json"
    manifest = queue._read_json(manifest_path)
    manifest["next_attempt_at"] = "2000-01-01T00:00:00+00:00"
    queue._write_json(manifest_path, manifest)
    confirmed = process_next(outbox=queue, store=store, settings=settings)
    assert confirmed["status"] == "completed"
    assert store.puts == (1 if phase == "after_commit" else 2)
    assert store.removes == 0 and len(store.objects) == (1 if phase == "after_commit" else 2)
    with SessionLocal() as db:
        assert len(PrintsRepository(db).list_print_files(record["record_id"])) == 1


def test_publication_guard_pins_claim_until_sql_commit(tmp_path, monkeypatch, sqlite_outer_transactions):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    settings = _settings(tmp_path)
    with SessionLocal() as db:
        record = PrintsRepository(db).create_print_record({
            "name": "SQL fence", "origin_compute_node_id": settings.compute_node_id,
        })
        db.commit()
    source = tmp_path / "drawing.pdf"
    source.write_bytes(b"immutable commit bytes")
    queue, operation = _enqueue(settings, source, record["record_id"])
    started, committed = Event(), Event()
    original_commit = Session.commit
    calls = 0
    future = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        def claim_after_wait():
            started.set()
            result = LocalNasOutbox.from_settings(settings).claim(operation["operation_id"])
            assert committed.is_set(), "claim was replaced before SQL commit"
            return result
        def commit(self):
            nonlocal calls, future
            calls += 1
            if calls == 2:
                manifest_path = queue.root / "processing" / operation["operation_id"] / "manifest.json"
                manifest = queue._read_json(manifest_path)
                manifest["claimed_at"] = "2000-01-01T00:00:00+00:00"
                queue._write_json(manifest_path, manifest)
                future = pool.submit(claim_after_wait)
                assert started.wait(1)
                assert not future.done(), "outbox fence was not held at final commit"
            original_commit(self)
            if calls == 2:
                committed.set()
        with monkeypatch.context() as patch:
            patch.setattr(Session, "commit", commit)
            state = process_next(outbox=queue, store=_Store(), settings=settings)
        replacement = future.result(timeout=2)
    assert state["status"] in ("completed", "processing")
    if replacement is not None:
        assert queue.verify_claimed(replacement).read_bytes() == source.read_bytes()
    with SessionLocal() as db:
        assert len(PrintsRepository(db).list_print_files(record["record_id"])) == 1
