"""Regression: large reports are offloaded to object storage instead of being
crammed into the PostgreSQL 1 GB jsonb `payload`. This reproduces the large-report
scenario the rest of the suite never exercises (it uses tiny synthetic data).

Uses an in-memory fake ObjectStore — no real MinIO needed.
"""
from uuid import uuid4
import hashlib
from copy import deepcopy

from domain.models.entities import ReportArtifact
from storage.db.session import SessionLocal
from storage.repositories.reports import ReportsRepository
from domain.services.session_reports import prepare_report, read_report_by_id


class _FakeStore:
    """In-memory stand-in for storage.object_store.minio_client.ObjectStore."""

    _blobs: dict = {}
    _available = True

    def __init__(self, *args, **kwargs):
        pass

    def is_available(self) -> bool:
        return _FakeStore._available

    def put_bytes_verified(self, bucket, name, data, content_type="application/json") -> str:
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


def test_timeline_preview_counts_real_rows_and_keeps_priority_without_mutation():
    from reporting.json_report.generator import _timeline_preview

    timeline = _big_report()["timeline"]
    timeline[321]["event_type"] = "pause"
    timeline[322]["event_type"] = "alarm"
    timeline[321]["ts"] = None
    original = deepcopy(timeline)
    preview = _timeline_preview(timeline)
    assert timeline == original
    assert all(event in preview for event in (timeline[321], timeline[322]))
    assert preview[-1]["event_type"] == "_truncated"
    assert f"→ {len(preview) - 1} shown." in preview[-1]["note"]
    assert len(preview) == 2001


def test_priority_preview_is_not_clipped_to_sampling_budget():
    from reporting.json_report.generator import _timeline_preview

    timeline = _big_report()["timeline"]
    for event in timeline[200:]:
        event["event_type"] = "alarm"
    preview = _timeline_preview(timeline, cap=250)
    assert preview[:-1] == timeline
    assert f"→ {len(timeline)} shown." in preview[-1]["note"]
    short = timeline[:20]
    assert _timeline_preview(short) is short


def test_large_report_offloaded_to_object_store(monkeypatch):
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", _FakeStore)
    _FakeStore._blobs = {}
    _FakeStore._available = True

    report = _big_report()
    report_id = report["report_id"]
    prepared = prepare_report(report)

    with SessionLocal() as db:
        ReportsRepository(db).save_prepared(report_id, prepared)
        db.commit()

        row = db.get(ReportArtifact, report_id)
        # full blob uploaded + pointer stored
        assert row.storage_uri.startswith(f"s3://reports/{report_id}/")
        object_name = row.storage_uri.removeprefix("s3://reports/")
        blob = _FakeStore._blobs[("reports", object_name)]
        assert object_name == f"{report_id}/{hashlib.sha256(blob).hexdigest()}.json"
        # DB payload is the bounded preview, not the full 5000-event timeline
        assert len(row.payload["timeline"]) <= 2001
        assert len(row.payload["timeline"]) < len(report["timeline"])
        # The read service releases SQL before expanding the full report.
        full = read_report_by_id(db, report_id)
        assert len(full["timeline"]) == 5000


def test_report_falls_back_to_payload_when_store_unavailable(monkeypatch):
    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", _FakeStore)
    _FakeStore._blobs = {}
    _FakeStore._available = False

    report = _big_report()
    report_id = report["report_id"]
    prepared = prepare_report(report)

    with SessionLocal() as db:
        ReportsRepository(db).save_prepared(report_id, prepared)
        db.commit()

        row = db.get(ReportArtifact, report_id)
        assert row.storage_uri is None
        # bounded payload still persisted (no 1 GB blow-up, no crash)
        assert len(row.payload["timeline"]) <= 2001
        # get_report falls back to the slim payload
        full = read_report_by_id(db, report_id)
        assert full["timeline"] == row.payload["timeline"]


def test_report_revisions_have_immutable_object_names(monkeypatch):
    from domain.services.session_reports import _read_object

    monkeypatch.setattr("storage.object_store.minio_client.ObjectStore", _FakeStore)
    _FakeStore._blobs = {}
    _FakeStore._available = True
    first = {"report_id": "same-report", "marker": "first"}
    second = {"report_id": "same-report", "marker": "second"}
    first_uri = prepare_report(first)["storage_uri"]
    second_uri = prepare_report(second)["storage_uri"]
    assert first_uri != second_uri
    assert prepare_report(first)["storage_uri"] == first_uri
    assert _read_object(first_uri) == first
    assert _read_object(second_uri) == second


def test_full_report_read_does_not_copy_discarded_sql_projection(monkeypatch):
    from domain.services import session_reports

    saved = {"report_id": "report-1", "session_id": "session-1", "timeline": [{"n": 1}]}
    artifact = {"report_id": "report-1", "session_id": "session-1",
                "storage_uri": "s3://reports/full.json", "payload": saved}
    monkeypatch.setattr(session_reports, "_read_object", lambda *a, **k: {**saved, "timeline": [{"n": 2}]})
    def unused_copy(*args):
        raise AssertionError("Unused copy")
    monkeypatch.setattr(session_reports, "deepcopy", unused_copy)
    assert session_reports._expand_report(artifact)["timeline"] == [{"n": 2}]
    assert artifact["payload"]["timeline"] == [{"n": 1}]


def test_sql_report_fallback_is_detached_from_publication_input():
    from domain.services.session_reports import _expand_report

    artifact = {"report_id": "report-1", "session_id": "session-1", "storage_uri": None,
                "payload": {"report_id": "report-1", "session_id": "session-1", "llm_runs": []}}
    returned = _expand_report(artifact)
    returned["llm_runs"].append({"content": "changed"})
    assert artifact["payload"]["llm_runs"] == []
