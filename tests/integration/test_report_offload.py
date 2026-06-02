"""Regression: large reports are offloaded to object storage instead of being
crammed into the PostgreSQL 1 GB jsonb `payload`. This reproduces the large-report
scenario the rest of the suite never exercises (it uses tiny synthetic data).

Uses an in-memory fake ObjectStore — no real MinIO needed.
"""
from uuid import uuid4

from domain.models.entities import ReportArtifact
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


class _FakeStore:
    """In-memory stand-in for storage.object_store.minio_client.ObjectStore."""

    _blobs: dict = {}
    _available = True

    def __init__(self, *args, **kwargs):
        pass

    def is_available(self) -> bool:
        return _FakeStore._available

    def put_bytes(self, bucket, name, data, content_type="application/json") -> str:
        _FakeStore._blobs[(bucket, name)] = data
        return f"s3://{bucket}/{name}"

    def get_bytes(self, bucket, name):
        return _FakeStore._blobs.get((bucket, name))


def _big_report() -> dict:
    timeline = [
        {"event_type": "tick", "ts": f"2026-06-01T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00", "n": i}
        for i in range(5000)
    ]
    return {
        "report_id": f"report_{uuid4().hex}",
        "session_id": None,
        "timeline": timeline,
        "version_metadata": {"v": 1},
    }


def test_large_report_offloaded_to_object_store(monkeypatch):
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", _FakeStore)
    _FakeStore._blobs = {}
    _FakeStore._available = True

    report = _big_report()
    report_id = report["report_id"]

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        repo.save_report(report)

        row = db.get(ReportArtifact, report_id)
        # full blob uploaded + pointer stored
        assert row.storage_uri == f"s3://reports/{report_id}.json"
        assert ("reports", f"{report_id}.json") in _FakeStore._blobs
        # DB payload is the bounded preview, not the full 5000-event timeline
        assert len(row.payload["timeline"]) <= 2001
        assert len(row.payload["timeline"]) < len(report["timeline"])
        # get_report transparently returns the FULL report from the store
        full = repo.get_report(report_id)
        assert len(full["timeline"]) == 5000


def test_report_falls_back_to_payload_when_store_unavailable(monkeypatch):
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", _FakeStore)
    _FakeStore._blobs = {}
    _FakeStore._available = False

    report = _big_report()
    report_id = report["report_id"]

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        repo.save_report(report)  # must not raise when the store is down

        row = db.get(ReportArtifact, report_id)
        assert row.storage_uri is None
        # bounded payload still persisted (no 1 GB blow-up, no crash)
        assert len(row.payload["timeline"]) <= 2001
        # get_report falls back to the slim payload
        full = repo.get_report(report_id)
        assert full["timeline"] == row.payload["timeline"]
