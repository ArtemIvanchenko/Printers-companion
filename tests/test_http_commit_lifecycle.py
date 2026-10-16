"""The actual API finishes its SQL unit of work before sending response headers."""

import asyncio
import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app
from core.config.settings import get_settings
from domain.models.jobs import BackgroundJob
from domain.models.prints import PrintRecord, PrintRecordFile
from domain.models.sessions import ImportJob
from storage.db.session import SessionLocal


def _client(events):
    async def observed(scope, receive, send):
        async def recorded_send(message):
            if message["type"] == "http.response.start":
                events.append(("headers", message["status"]))
            await send(message)
        await app(scope, receive, recorded_send)
    return TestClient(observed, raise_server_exceptions=False)


@pytest.mark.parametrize("endpoint,payload,table,status", [
    ("/models/defect-risk/retrain", None, BackgroundJob, 200),
    ("/prints/recalibrate", None, BackgroundJob, 202),
    ("/prints", {"name": "Commit before response"}, PrintRecord, 200),
])
@pytest.mark.parametrize("failed", [False, True])
def test_json_response_headers_follow_successful_commit_or_failure(
    monkeypatch, endpoint, payload, table, status, failed,
):
    events = []
    client = _client(events)
    commit = Session.commit
    close = Session.close

    def committing(self):
        events.append(("commit", None))
        if failed:
            raise OperationalError("COMMIT", {}, ConnectionError("synthetic commit failure"))
        return commit(self)

    def closing(self):
        events.append(("close", None))
        return close(self)

    with monkeypatch.context() as observed:
        observed.setattr(Session, "commit", committing)
        observed.setattr(Session, "close", closing)
        response = client.post(endpoint, json=payload)
    expected = 500 if failed else status
    assert response.status_code == expected, response.text
    assert [item for item in events if item[0] == "headers"] == [("headers", expected)]
    assert events.index(("commit", None)) < events.index(("headers", expected))
    assert events.index(("close", None)) < events.index(("headers", expected))
    with SessionLocal() as db:
        rows = list(db.scalars(select(table)).all())
        assert len(rows) == (0 if failed else 1)
        if rows and table is BackgroundJob:
            assert rows[0].owner_node_id == get_settings().compute_node_id
            assert rows[0].status == "pending"


def test_direct_sql_dependency_commit_failure_returns_500_and_preserves_import_state(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(tmp_path))
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    client = TestClient(app, raise_server_exceptions=False)
    uploaded = client.post("/upload/logs", files=[("files", ("01.04.2026_time.log", b"own source", "text/plain"))])
    assert uploaded.status_code == 200, uploaded.text
    job_id = uploaded.json()["jobs"][0]["import_job_id"]

    def committing(self):
        raise OperationalError("COMMIT", {}, ConnectionError("synthetic commit failure"))

    with monkeypatch.context() as failing:
        failing.setattr(Session, "commit", committing)
        response = client.post(f"/imports/{job_id}/confirm")
    assert response.status_code == 500, response.text
    with SessionLocal() as db:
        job = db.get(ImportJob, job_id)
        assert job.status == "awaiting_operator_confirmation"
        assert job.confirmed_by is None and job.confirmed_at is None
        assert job.lease_owner is None and job.lease_generation == 0


class MemoryStore:
    objects = {}
    events = None

    def __init__(self, *args, **kwargs):
        pass

    def is_available(self):
        return True

    def put_file_verified(self, bucket, name, path, expected_sha256, expected_size, **kwargs):
        payload = Path(path).read_bytes()
        assert len(payload) == expected_size
        assert hashlib.sha256(payload).hexdigest() == expected_sha256
        self.objects[bucket, name] = payload
        return f"s3://{bucket}/{name}"

    def open_stream(self, bucket, name):
        payload = self.objects[bucket, name]
        def chunks():
            assert ("close", None) in self.events, "stream used a live request dependency"
            self.events.append(("bytes", None))
            yield payload[:4]
            yield payload[4:]
        return chunks()

    def remove_object(self, bucket, name):
        with SessionLocal() as db:
            assert not list(db.scalars(select(PrintRecordFile)).all())
        self.events.append(("cleanup", None))
        return self.objects.pop((bucket, name), None) is not None


@pytest.fixture
def attachment(monkeypatch):
    MemoryStore.objects = {}
    MemoryStore.events = []
    monkeypatch.setattr("api.routes.prints.ObjectStore", MemoryStore)
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post("/prints", json={"name": "Stream commit lifecycle"})
    assert created.status_code == 200, created.text
    record_id = created.json()["record_id"]
    response = client.post(f"/prints/{record_id}/files",
        files={"file": ("drawing.pdf", b"own synthetic document", "application/pdf")},
        data={"file_type": "doc"})
    assert response.status_code == 200, response.text
    return client, record_id, response.json()["file_id"]


@pytest.mark.parametrize("failed", [False, True])
def test_file_stream_starts_only_after_request_dependency_finishes(attachment, monkeypatch, failed):
    _, record_id, file_id = attachment
    events = MemoryStore.events
    client = _client(events)
    commit, close = Session.commit, Session.close

    def committing(self):
        events.append(("commit", None))
        if failed:
            raise OperationalError("COMMIT", {}, ConnectionError("synthetic commit failure"))
        return commit(self)

    def closing(self):
        events.append(("close", None))
        return close(self)

    with monkeypatch.context() as observed:
        observed.setattr(Session, "commit", committing)
        observed.setattr(Session, "close", closing)
        response = client.get(f"/prints/{record_id}/files/{file_id}/download")
    assert response.status_code == (500 if failed else 200)
    if failed:
        assert not [entry for entry in events if entry[0] == "bytes"]
    else:
        assert response.content == b"own synthetic document"
        assert events.index(("close", None)) < events.index(("headers", 200)) < events.index(("bytes", None))
    assert len(MemoryStore.objects) == 1


@pytest.mark.parametrize("failed", [False, True])
def test_delete_background_cleanup_follows_committed_deletion(attachment, monkeypatch, failed):
    _, record_id, file_id = attachment
    events = MemoryStore.events
    client = _client(events)

    def committed(self):
        events.append(("committed", None))

    def failing_commit(self):
        raise OperationalError("COMMIT", {}, ConnectionError("synthetic delete commit failure"))

    event.listen(Session, "after_commit", committed)
    try:
        with monkeypatch.context() as observed:
            if failed:
                observed.setattr(Session, "commit", failing_commit)
            response = client.delete(f"/prints/{record_id}/files/{file_id}")
    finally:
        event.remove(Session, "after_commit", committed)
    assert response.status_code == (500 if failed else 200), response.text
    if failed:
        assert not [item for item in events if item[0] == "cleanup"]
        assert len(MemoryStore.objects) == 1
    else:
        assert events.index(("committed", None)) < events.index(("cleanup", None))
        assert not MemoryStore.objects


def test_export_stream_has_detached_data_and_a_closed_sql_session(monkeypatch):
    client = TestClient(app)
    created = client.post("/prints", json={"name": "Detached export"})
    assert created.status_code == 200
    events = []
    close = Session.close
    def closing(self):
        events.append(("close", None))
        return close(self)
    with monkeypatch.context() as observed:
        observed.setattr(Session, "close", closing)
        response = _client(events).get("/export/download?cat=operator")
    assert response.status_code == 200
    assert response.json()["operator"]["print_records"][0]["record_id"] == created.json()["record_id"]
    assert events.index(("close", None)) < events.index(("headers", 200))


def test_sse_uses_its_pollers_transaction_not_a_request_sql_session(monkeypatch):
    from api.routes import prints

    monkeypatch.setattr(prints, "_PRINT_SYNC_SUBSCRIBERS", set())
    monkeypatch.setattr(prints, "_PRINT_SYNC_TASK", None)
    monkeypatch.setattr(prints, "_PRINT_SYNC_STATE", None)

    async def request():
        disconnect = asyncio.Event()
        frames = []
        initial = True
        async def receive():
            nonlocal initial
            if initial:
                initial = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            frames.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                raise asyncio.CancelledError  # Client disconnects after its first SSE frame.
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": "/prints/events", "raw_path": b"/prints/events", "root_path": "",
                 "query_string": b"", "headers": [], "client": ("test", 1), "server": ("test", 80)}
        try:
            await asyncio.wait_for(app(scope, receive, send), timeout=3)
        except asyncio.CancelledError:
            pass
        finally:
            monitor = prints._PRINT_SYNC_TASK
            if monitor:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
        assert frames[0]["type"] == "http.response.start" and frames[0]["status"] == 200
        assert any(b"event: print-records" in frame.get("body", b"") for frame in frames)
        assert not prints._PRINT_SYNC_SUBSCRIBERS
    asyncio.run(request())
