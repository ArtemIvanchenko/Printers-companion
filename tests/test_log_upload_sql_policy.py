"""Registration/recovery distinguish SQL bugs from evidenced disconnection."""

from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.errors import ConnectionFailure
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from api.main import app
from core.config.settings import get_settings
from domain.models.sessions import ImportJob
from domain.services.importing import recovery
from domain.services.importing.uploads import LogUpload, upload_log_batch
from domain.services.print_cards.contracts import CardError
from storage.db.session import SessionLocal, engine, is_database_unavailable
from storage.repositories.prints_repo import PrintsRepository


LOG_BYTES = b"2026-07-17 10:00:00 start\n2026-07-17 10:00:01 finish\n"
SQL_FAILURES = [
    pytest.param("SELECT RAISE(ABORT, 'registration constraint failed')", IntegrityError,
                 id="sqlite_constraint"),
    pytest.param("SELECT nonexistent_registration_function()", OperationalError,
                 id="sqlite_programming"),
]


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(tmp_path))
    monkeypatch.setattr(settings, "compute_node_id", "sql-policy-owner")
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    return tmp_path


@contextmanager
def rejected_registration(sql):
    """Let SQLite itself reject a normal production ImportJob INSERT."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_upload_registration BEFORE INSERT ON import_jobs "
            f"BEGIN {sql}; END"
        )
    failures = []
    def failed(context):
        failures.append(context.sqlalchemy_exception)
    event.listen(engine, "handle_error", failed)
    try:
        yield failures
    finally:
        event.remove(engine, "handle_error", failed)
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER reject_upload_registration")


def retained_receipt(inbox):
    receipts = [path for path in (inbox / "incoming").iterdir() if path.suffix == ".json"]
    assert len(receipts) == 1
    receipt_path = receipts[0]
    receipt = json.loads(receipt_path.read_text())
    assert receipt["owner_node_id"] == "sql-policy-owner"
    assert receipt["job"]["owner_node_id"] == "sql-policy-owner"
    assert receipt["job"]["status"] == "awaiting_operator_confirmation"
    assert receipt["job"]["lease_generation"] == 0
    assert receipt["job"]["lease_owner"] is None
    source = Path(receipt["source_path"]) / "17.07.2026.log"
    assert source.read_bytes() == LOG_BYTES
    assert receipt["files"][0]["checksum"] == hashlib.sha256(LOG_BYTES).hexdigest()
    with SessionLocal() as db:
        assert db.scalars(select(ImportJob)).all() == []
    return receipt_path, receipt


@pytest.mark.parametrize("sql,error_type", SQL_FAILURES)
@pytest.mark.parametrize("card_specific", [False, True])
def test_log_upload_sql_failure_is_http_500_and_retains_attention_receipt(
    inbox, sql, error_type, card_specific,
):
    record_id = None
    if card_specific:
        with SessionLocal() as db:
            record_id = PrintsRepository(db).create_print_record({
                "name": "SQL publication check", "origin_compute_node_id": "sql-policy-owner",
            })["record_id"]
            db.commit()
    url = f"/prints/{record_id}/import-logs" if card_specific else "/upload/logs"
    with rejected_registration(sql) as failures:
        response = TestClient(app, raise_server_exceptions=False).post(
            url, files=[("files", ("17.07.2026.log", LOG_BYTES, "text/plain"))],
        )
    assert len(failures) == 1 and isinstance(failures[0], error_type)
    assert not is_database_unavailable(failures[0])
    assert response.status_code == 500, response.text
    path, receipt = retained_receipt(inbox)
    assert receipt["print_record_id"] == receipt["job"]["print_record_id"] == record_id
    assert receipt["recovery"]["state"] == "needs_attention"
    assert "retry_after" not in receipt["recovery"]
    assert recovery.recover_upload_receipt(path, settings=get_settings())["state"] == "skipped"
    retained_receipt(inbox)


@pytest.fixture
def disconnected_receipt(inbox, monkeypatch):
    def disconnected(self):
        raise OperationalError("COMMIT", {}, ConnectionFailure("commit connection lost"))
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", disconnected)
        with SessionLocal() as db, pytest.raises(CardError) as failure:
            upload_log_batch(PrintsRepository(db), [LogUpload("17.07.2026.log", io.BytesIO(LOG_BYTES))],
                             settings=get_settings())
        assert failure.value.code == "storage_unavailable"
    return retained_receipt(inbox)


@pytest.mark.parametrize("sql,error_type", SQL_FAILURES)
def test_recovery_sql_failure_needs_attention_without_repeated_registration(
    inbox, disconnected_receipt, sql, error_type,
):
    path, receipt = disconnected_receipt
    with rejected_registration(sql) as failures:
        result = recovery.recover_upload_receipt(path, settings=get_settings(), now=100)
    assert len(failures) == 1 and isinstance(failures[0], error_type)
    assert not is_database_unavailable(failures[0])
    assert result["state"] == "needs_attention"
    saved = json.loads(path.read_text())
    assert saved["recovery"]["state"] == "needs_attention"
    assert "retry_after" not in saved["recovery"]
    assert recovery.recover_upload_receipt(path, settings=get_settings(), now=10000) == {
        "state": "skipped", "job_id": receipt["import_job_id"],
    }
    retained_receipt(inbox)


def test_real_connection_failure_retains_backoff_and_recovers_same_job(
    inbox, disconnected_receipt, monkeypatch,
):
    path, receipt = disconnected_receipt
    def disconnected(self):
        raise OperationalError("COMMIT", {}, ConnectionFailure("commit connection lost"))
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", disconnected)
        assert recovery.recover_upload_receipt(path, settings=get_settings(), now=100)["state"] == "deferred"
    saved = json.loads(path.read_text())
    assert saved["recovery"]["retry_after"] > 100
    retained_receipt(inbox)
    result = recovery.recover_upload_receipt(path, settings=get_settings(), now=10000)
    assert result == {"state": "registered", "job_id": receipt["import_job_id"]}
    with SessionLocal() as db:
        jobs = db.scalars(select(ImportJob)).all()
        assert len(jobs) == 1
        assert jobs[0].import_job_id == receipt["import_job_id"]
        assert jobs[0].owner_node_id == "sql-policy-owner"
        assert jobs[0].lease_generation == 0
        assert jobs[0].status == "awaiting_operator_confirmation"
    assert (Path(receipt["source_path"]) / "17.07.2026.log").read_bytes() == LOG_BYTES
