"""Regression contracts for published reads and owner-local analysis requests."""

from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from api.main import app
from core.config.settings import get_settings
from domain.enums.common import ImportJobStatus
from domain.models.sessions import BuildSession, ImportJob, ReportArtifact
from domain.services.session_reports import SessionReportError, read_report, read_report_by_id, enhance_report
from reporting.llm.providers.base import LLMResult
from storage.db.session import SessionLocal, engine, session_scope
from storage.repositories.runtime import RuntimeRepository
from storage.repositories.reports import ReportsRepository


def seed(*, owner=None, snapshot=None, publication=None, job=False, legacy_job=False):
    owner = owner or get_settings().compute_node_id
    group = {"classification": "REAL_PRINT", "features": {"layers": 10}}
    report = {"report_id": "report-1", "session_id": "session-1",
              "timeline": [{"event_type": "first"}], "phase_segments": [],
              "file_inventory": [], "anomalies": [], "hypotheses": [],
              "data_quality": {"parse_diagnostics": []}}
    if snapshot:
        group["analysis_snapshot"] = snapshot
        report["analysis_snapshot"] = snapshot
    if publication:
        group["timing_publication_id"] = publication
        report["timing_publication"] = {"publication_id": publication}
    context = {"runtime_payload": {"files": [], "group": group}}
    if job and not legacy_job:
        context["timing_publication"] = {"source": {"import_job_id": "import-1"}}
    with session_scope() as db:
        db.add(BuildSession(session_id="session-1", origin_compute_node_id=owner, context=context))
        db.add(ReportArtifact(report_id="report-1", session_id="session-1", report_type="session", payload=report))
        if job:
            db.add(ImportJob(import_job_id="import-1", owner_node_id=owner,
                             source_path="/does-not-need-to-be-opened", source_name="logs",
                             status=ImportJobStatus.done, session_ids=["session-1"]))
    return report


@pytest.mark.parametrize("owner", [None, "another-pc"])
def test_get_reads_published_report_without_parse_or_write(monkeypatch, owner):
    saved = seed(owner=owner)
    monkeypatch.setattr("domain.services.session_sources.rehydrate_session_sources", lambda *a, **k: pytest.fail("GET reparsed files"))
    writes = []

    def track(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", track)
    try:
        response = TestClient(app).get("/sessions/session-1/timeline")
    finally:
        event.remove(engine, "before_cursor_execute", track)
    assert response.status_code == 200
    assert response.json() == saved["timeline"]
    assert not writes


def test_republished_report_visible_without_process_invalidation():
    seed()
    client = TestClient(app)
    assert client.get("/sessions/session-1/timeline").json()[0]["event_type"] == "first"
    with session_scope() as db:
        db.add(ReportArtifact(report_id="report-2", session_id="session-1", report_type="session",
                              generated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
                              payload={"report_id": "report-2", "session_id": "session-1",
                                       "timeline": [{"event_type": "second"}]}))
    assert client.get("/sessions/session-1/timeline").json()[0]["event_type"] == "second"


@pytest.mark.parametrize("mismatch", ["analysis", "timing"])
def test_mixed_publications_fail_closed(mismatch):
    seed(snapshot={"analysis_id": "one"}, publication="one")
    with session_scope() as db:
        row = db.get(ReportArtifact, "report-1")
        row.payload = {**row.payload, ("analysis_snapshot" if mismatch == "analysis" else "timing_publication"):
                       ({"analysis_id": "two"} if mismatch == "analysis" else {"publication_id": "two"})}
    assert TestClient(app).get("/sessions/session-1/timeline").status_code == 409


@pytest.mark.parametrize("by_id", [False, True])
def test_minio_expansion_releases_sql_first(by_id):
    saved = seed()
    with session_scope() as db:
        db.get(ReportArtifact, "report-1").storage_uri = "s3://reports/full.json"
    held = set()
    def checkout(connection, record, proxy):
        held.add(id(connection))
    def checkin(connection, record):
        held.discard(id(connection))
    class Store:
        def get_bytes(self, bucket, name):
            assert not held
            return json.dumps(saved).encode()
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        with SessionLocal() as db:
            reader, identifier = (read_report_by_id, "report-1") if by_id else (read_report, "session-1")
            assert reader(db, identifier, object_store_factory=Store) == saved
            assert not db.in_transaction()
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_minio_wrong_object_is_rejected():
    seed()
    with session_scope() as db:
        db.get(ReportArtifact, "report-1").storage_uri = "s3://reports/wrong.json"
    class Store:
        def get_bytes(self, *args):
            return b'{"report_id":"someone-else", "session_id":"other"}'
    with SessionLocal() as db, pytest.raises(SessionReportError) as error:
        read_report(db, "session-1", object_store_factory=Store)
    assert error.value.code == "conflict"


def test_minio_outage_reads_bounded_sql_projection():
    saved = seed()
    with session_scope() as db:
        db.get(ReportArtifact, "report-1").storage_uri = "s3://reports/full.json"
    class Store:
        def get_bytes(self, *args):
            raise ConnectionError("NAS unavailable")
    with SessionLocal() as db:
        assert read_report(db, "session-1", object_store_factory=Store) == saved


def test_report_id_keeps_historical_publication():
    saved = seed(snapshot={"analysis_id": "old"})
    with session_scope() as db:
        session = db.get(BuildSession, "session-1")
        session.context = {"runtime_payload": {"group": {"analysis_snapshot": {"analysis_id": "new"}}}}
        db.add(ReportArtifact(report_id="report-2", session_id="session-1", report_type="session",
            generated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            payload={**saved, "report_id": "report-2", "analysis_snapshot": {"analysis_id": "new"}}))
    client = TestClient(app)
    assert client.get("/reports/report-1").json() == saved
    assert client.get("/sessions/session-1/timeline").status_code == 200


def test_report_id_rejects_wrong_sql_identity():
    seed()
    with session_scope() as db:
        row = db.get(ReportArtifact, "report-1")
        row.payload = {**row.payload, "report_id": "wrong"}
    assert TestClient(app).get("/reports/report-1").status_code == 409


@pytest.mark.parametrize("change", [None, "report", "session", "owner", "storage"])
def test_llm_releases_sql_and_rechecks_publication(change):
    import asyncio

    saved = seed(snapshot={"analysis_id": "current"}, publication="current")
    blobs = {}
    held = set()
    def checkout(connection, record, proxy):
        held.add(id(connection))
    def checkin(connection, record):
        held.discard(id(connection))
    class Store:
        def is_available(self):
            assert not held
            return change != "storage"
        def put_bytes_verified(self, bucket, name, data):
            assert not held
            blobs[(bucket, name)] = data
            return f"s3://{bucket}/{name}"
        def get_bytes(self, bucket, name):
            assert not held
            return blobs[(bucket, name)]
    class Provider:
        async def generate_markdown(self, evidence):
            assert not held
            if change in {"report", "session", "owner"}:
                with session_scope() as other:
                    if change == "report":
                        row = other.get(ReportArtifact, "report-1")
                        row.payload = {**row.payload, "llm_runs": [{"content": "parallel"}]}
                    elif change == "session":
                        row = other.get(BuildSession, "session-1")
                        row.context = {**row.context, "operator_changed": True}
                    else:
                        other.get(BuildSession, "session-1").origin_compute_node_id = "other-pc"
            return LLMResult(success=True, content="Дополнение", provider="test")
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        with SessionLocal() as db:
            call = enhance_report(db, "report-1", compute_node_id=get_settings().compute_node_id,
                                  provider=Provider(), object_store_factory=Store)
            if change:
                with pytest.raises(SessionReportError) as error:
                    asyncio.run(call)
                assert error.value.code == {"owner": "forbidden", "storage": "storage_unavailable"}.get(change, "conflict")
            else:
                assert asyncio.run(call)["llm"]["content"] == "Дополнение"
            assert not db.in_transaction()
        with SessionLocal() as db:
            published = read_report_by_id(db, "report-1", object_store_factory=Store)
        if change is None:
            assert published["llm_markdown"] == "Дополнение"
            assert len(published["llm_runs"]) == 1
        elif change == "report":
            assert published["llm_runs"] == [{"content": "parallel"}]
        else:
            assert published == saved
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_missing_published_report_does_not_trigger_local_analysis(monkeypatch):
    seed()
    with session_scope() as db:
        db.delete(db.get(ReportArtifact, "report-1"))
    monkeypatch.setattr("domain.services.session_sources.rehydrate_session_sources", lambda *a, **k: pytest.fail("must not parse"))
    assert TestClient(app).get("/sessions/session-1/anomalies").status_code == 409


def test_sessions_paginate_in_sql_and_skip_unpublished_rows(monkeypatch):
    with session_scope() as db:
        for number in range(8):
            db.add(BuildSession(session_id=f"session-{number}", context={"runtime_payload": {"group": {"number": number}}},
                                created_at=datetime(2026, 1, number + 1, tzinfo=timezone.utc)))
        db.add_all([BuildSession(session_id="empty", context={}),
                    BuildSession(session_id="empty-payload", context={"runtime_payload": {}})])
    monkeypatch.setattr(RuntimeRepository, "list_session_payloads", lambda *a, **k: pytest.fail("full archive load"))
    statements = []
    def track(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.upper())
    event.listen(engine, "before_cursor_execute", track)
    try:
        response = TestClient(app).get("/sessions?skip=2&limit=3")
    finally:
        event.remove(engine, "before_cursor_execute", track)
    assert response.status_code == 200
    assert response.json()["total"] == 8
    assert [row["number"] for row in response.json()["items"]] == [5, 4, 3]
    assert any("LIMIT" in sql and "OFFSET" in sql for sql in statements)


def test_sessions_select_groups_without_loading_large_source_inventory():
    from domain.services.session_reports import list_sessions

    group = {"classification": "REAL_PRINT", "features": {"layers": 10},
             "analysis_snapshot": {"analysis_id": "one"}}
    payloads = {
        "session-a": {"group": group, "files": [{"raw_excerpt": "source" * 200_000}]},
        "session-b": {"files": []},
        "session-c": {"group": None},
        "session-d": {"group": {}},
    }
    with session_scope() as db:
        db.add_all(BuildSession(session_id=sid, context={"runtime_payload": payload},
                                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
                   for sid, payload in payloads.items())
        db.add_all([BuildSession(session_id="empty", context={}),
                    BuildSession(session_id="empty-payload", context={"runtime_payload": {}}),
                    BuildSession(session_id="null-payload", context={"runtime_payload": None})])
    projections = []

    def track(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and statement.lstrip().upper().startswith("SELECT"):
            columns = tuple(compiled.statement.selected_columns)
            if len(columns) == 2:
                projections.append(columns[1])

    event.listen(engine, "before_cursor_execute", track)
    try:
        with SessionLocal() as db:
            result = list_sessions(db, skip=0, limit=10)
            assert not db.in_transaction()
    finally:
        event.remove(engine, "before_cursor_execute", track)
    assert result == {
        "items": [{"session_id": sid, **(payloads[sid].get("group") or {})}
                  for sid in sorted(payloads, reverse=True)],
        "total": 4, "skip": 0, "limit": 10, "returned": 4,
    }
    assert len(projections) == 1
    assert projections[0].compare(BuildSession.context["runtime_payload"]["group"])


def test_ingest_v2_only_queues_committed_local_work(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(tmp_path))
    monkeypatch.setattr("domain.services.ingestion.IngestionService.parse", lambda *a, **k: pytest.fail("HTTP parsed logs"))
    client = TestClient(app)
    first = client.post("/sessions/ingest", json={"folder": str(tmp_path / "new-batch")})
    assert first.status_code == 202
    body = first.json()
    assert body["contract_version"] == 2
    assert body["groups"] == []
    with SessionLocal() as db:
        job = db.get(ImportJob, body["job_id"])
        assert job.status == ImportJobStatus.checking_stability
        assert job.owner_node_id == get_settings().compute_node_id
        assert db.scalars(select(BuildSession)).all() == []
    second = client.post("/sessions/ingest", json={"folder": str(tmp_path / "new-batch")})
    assert second.json()["job_id"] == body["job_id"]


@pytest.mark.parametrize("payload,status", [({}, 422), ({"folder": "/outside-raw-root"}, 403),
                                          ({"folder": "any", "session_id": "spoofed"}, 422)])
def test_ingest_rejects_unsafe_or_ambiguous_requests(tmp_path, monkeypatch, payload, status):
    monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(tmp_path))
    assert TestClient(app).post("/sessions/ingest", json=payload).status_code == status


def test_ingest_rejects_private_browser_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(tmp_path))
    assert TestClient(app).post("/sessions/ingest", json={
        "folder": str(tmp_path / ".browser-upload-private"),
    }).status_code == 403


@pytest.mark.parametrize("legacy", [False, True])
def test_reanalysis_queues_original_job_without_opening_paths(monkeypatch, legacy):
    seed(job=True, legacy_job=legacy)
    monkeypatch.setattr("domain.services.session_sources.rehydrate_session_sources", lambda *a, **k: pytest.fail("HTTP opened logs"))
    client = TestClient(app)
    for route in ("analyze", "reanalyze"):
        response = client.post(f"/sessions/session-1/{route}")
        assert response.status_code == 202
        assert response.json()["job_id"] == "import-1"
        assert "reanalyzed" not in response.json()
    with SessionLocal() as db:
        job = db.get(ImportJob, "import-1")
        assert job.status == ImportJobStatus.checking_stability
        assert not job.session_ids
        assert len([item for item in job.audit_trail if item["action"] == "session_analysis_requested"]) == 1


def test_reanalysis_refuses_foreign_owner():
    seed(owner="foreign", job=True)
    assert TestClient(app).post("/sessions/session-1/reanalyze").status_code == 403
    with SessionLocal() as db:
        assert db.get(ImportJob, "import-1").status == ImportJobStatus.done


def test_legacy_session_without_import_job_requires_explicit_reimport():
    seed()
    response = TestClient(app).post("/sessions/session-1/reanalyze")
    assert response.status_code == 409
    assert "исходного задания" in response.json()["detail"]


def test_reanalysis_does_not_replace_live_worker_lease():
    seed(job=True)
    with session_scope() as db:
        job = db.get(ImportJob, "import-1")
        job.status = ImportJobStatus.importing
        job.lease_owner = "worker-1"
        job.lease_generation = 7
        job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    response = TestClient(app).post("/sessions/session-1/reanalyze")
    assert response.status_code == 202
    with SessionLocal() as db:
        job = db.get(ImportJob, "import-1")
        assert job.lease_owner == "worker-1"
        assert job.lease_generation == 7


def test_reanalysis_commit_failure_does_not_publish_success(monkeypatch):
    seed(job=True)
    def fail(self):
        raise OperationalError("commit", {}, RuntimeError("offline"))
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail)
        response = TestClient(app, raise_server_exceptions=False).post("/sessions/session-1/reanalyze")
    assert response.status_code == 503
    with SessionLocal() as db:
        assert db.get(ImportJob, "import-1").status == ImportJobStatus.done


def test_operator_report_closes_sql_before_presentation(monkeypatch):
    seed()
    held = set()
    def checkout(connection, record, proxy):
        held.add(id(connection))
    def checkin(connection, record):
        held.discard(id(connection))
    def build(**kwargs):
        assert not held
        return {"session_id": kwargs["session_id"]}
    monkeypatch.setattr("domain.services.operator_report.build_operator_report", build)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        response = TestClient(app).get("/sessions/session-1/operator-report")
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert response.status_code == 200


def test_markdown_generation_is_presentation_only(monkeypatch):
    seed()
    monkeypatch.setattr(ReportsRepository, "save_prepared", lambda *a, **k: pytest.fail("formatting wrote report"))
    monkeypatch.setattr("domain.services.session_sources.rehydrate_session_sources", lambda *a, **k: pytest.fail("formatting opened raw logs"))
    monkeypatch.setattr("reporting.markdown_report.generator.generate_markdown_report", lambda report: "# Published")
    response = TestClient(app).post("/sessions/session-1/reports/generate")
    assert response.status_code == 200
    assert response.json()["markdown"] == "# Published"
    assert response.json()["report_id"] == "report-1"
    with SessionLocal() as db:
        assert "markdown" not in db.get(ReportArtifact, "report-1").payload


def test_new_report_cannot_be_silently_attached_to_legacy_group():
    seed()
    with session_scope() as db:
        report = db.get(ReportArtifact, "report-1")
        report.payload = {**report.payload, "analysis_snapshot": {"analysis_id": "new"}}
    assert TestClient(app).get("/sessions/session-1/timeline").status_code == 409


def test_legacy_multi_session_link_survives_retry_of_first_session():
    seed(job=True, legacy_job=True)
    with session_scope() as db:
        db.add(BuildSession(session_id="session-2", origin_compute_node_id=get_settings().compute_node_id,
                            context={"runtime_payload": {"files": [], "group": {}}}))
        db.get(ImportJob, "import-1").session_ids = ["session-1", "session-2"]
    client = TestClient(app)
    assert client.post("/sessions/session-1/reanalyze").status_code == 202
    second = client.post("/sessions/session-2/reanalyze")
    assert second.status_code == 202
    assert second.json()["job_id"] == "import-1"


def test_ingest_symlink_cannot_escape_local_root(tmp_path, monkeypatch):
    root = tmp_path / "incoming"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(root))
    assert TestClient(app).post("/sessions/ingest", json={"folder": str(root / "escape")}).status_code == 403
