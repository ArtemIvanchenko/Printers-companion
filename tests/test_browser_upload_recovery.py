"""Recovery never guesses identity, deletes inputs, or replaces live leases."""

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from core.config.settings import get_settings
from domain.enums.common import ImportJobStatus
from domain.models.prints import PrintRecord
from domain.models.sessions import ImportJob
from domain.services.importing import recovery
from domain.services.importing.uploads import LogUpload, upload_log_batch
from domain.services.print_cards.contracts import CardError
from storage.db.session import SessionLocal, engine, session_scope
from storage.repositories.prints_repo import PrintsRepository


@pytest.fixture
def recoverable(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(tmp_path))
    monkeypatch.setattr(settings, "compute_node_id", "recovery-owner")
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)

    def create(*, acknowledgement_lost=False, with_card=True):
        record_id = None
        if with_card:
            with session_scope() as db:
                record_id = PrintsRepository(db).create_print_record({
                    "name": "Recovery", "origin_compute_node_id": settings.compute_node_id,
                    "metadata_json": {"keep": "operator value"},
                })["record_id"]
        original_commit = Session.commit
        def uncertain(self):
            if acknowledgement_lost:
                original_commit(self)
            raise OperationalError("COMMIT", {}, RuntimeError("NAS disconnect"))
        with monkeypatch.context() as patch:
            patch.setattr(Session, "commit", uncertain)
            with SessionLocal() as db, pytest.raises(CardError):
                upload_log_batch(PrintsRepository(db), [LogUpload("17.07.2026_time.log", io.BytesIO(b"verified log"))],
                                 settings=settings, record_id=record_id)
        receipt = next((tmp_path / "incoming").glob("*.json"))
        payload = json.loads(receipt.read_text())
        return receipt, payload, record_id
    return create


def replace_receipt(path, payload):
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_failed_commit_recovers_exact_job_and_metadata_once(recoverable):
    path, receipt, record_id = recoverable()
    result = recovery.recover_upload_receipt(path, settings=get_settings())
    assert result == {"state": "registered", "job_id": receipt["import_job_id"]}
    with SessionLocal() as db:
        jobs = db.scalars(select(ImportJob)).all()
        assert len(jobs) == 1
        assert jobs[0].import_job_id == receipt["import_job_id"]
        assert jobs[0].print_record_id == record_id
        assert jobs[0].status == ImportJobStatus.awaiting_operator_confirmation
        card = db.get(PrintRecord, record_id)
        revision = card.revision
        assert card.printed_at.date().isoformat() == "2026-07-17"
        assert card.metadata_json["keep"] == "operator value"
    assert recovery.recover_upload_receipt(path, settings=get_settings())["state"] == "skipped"
    with SessionLocal() as db:
        assert db.get(PrintRecord, record_id).revision == revision
    assert (Path(receipt["source_path"]) / "17.07.2026_time.log").read_bytes() == b"verified log"


def test_unknown_commit_is_looked_up_before_hash_and_preserves_live_lease(recoverable, monkeypatch):
    path, receipt, _ = recoverable(acknowledgement_lost=True)
    with session_scope() as db:
        job = db.get(ImportJob, receipt["import_job_id"])
        job.status = ImportJobStatus.importing
        job.lease_owner = "alive-worker"
        job.lease_generation = 9
        job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    monkeypatch.setattr(recovery, "_verify_batch", lambda *a: pytest.fail("already registered batch was hashed"))
    assert recovery.recover_upload_receipt(path, settings=get_settings())["state"] == "registered"
    with SessionLocal() as db:
        job = db.get(ImportJob, receipt["import_job_id"])
        assert job.lease_owner == "alive-worker"
        assert job.lease_generation == 9
        assert job.status == ImportJobStatus.importing


@pytest.mark.parametrize("mutation", ["checksum", "extra_file", "missing_file", "symlink", "traversal"])
def test_tampered_batch_is_quarantined_without_registration_or_deletion(recoverable, mutation, tmp_path):
    path, receipt, _ = recoverable()
    batch = Path(receipt["source_path"])
    source = batch / "17.07.2026_time.log"
    if mutation == "checksum":
        source.write_bytes(b"tampered log")
    elif mutation == "extra_file":
        (batch / "unexpected.log").write_bytes(b"unknown")
    elif mutation == "missing_file":
        source.rename(batch.parent / "operator-moved.log")
    elif mutation == "symlink":
        original = tmp_path / "original.log"
        source.rename(original)
        source.symlink_to(original)
    else:
        receipt["files"][0]["stored_name"] = "../escape.log"
        replace_receipt(path, receipt)
    result = recovery.recover_upload_receipt(path, settings=get_settings())
    assert result["state"] == "needs_attention"
    with SessionLocal() as db:
        assert db.scalars(select(ImportJob)).all() == []
    assert path.exists() and batch.exists()


@pytest.mark.parametrize("mutation", ["foreign_owner", "source_path", "job_identity", "changed_card_owner", "deleted_card"])
def test_wrong_identity_or_card_cannot_be_recovered(recoverable, monkeypatch, mutation):
    path, receipt, record_id = recoverable()
    if mutation == "foreign_owner":
        receipt["owner_node_id"] = "foreign-pc"
    elif mutation == "source_path":
        receipt["source_path"] = "/not-the-adjacent-private-batch"
    elif mutation == "job_identity":
        receipt["job"]["owner_node_id"] = "foreign-pc"
    else:
        with session_scope() as db:
            row = db.get(PrintRecord, record_id)
            if mutation == "deleted_card":
                db.delete(row)
            else:
                row.origin_compute_node_id = "foreign-pc"
    replace_receipt(path, receipt)
    if mutation in ("foreign_owner", "source_path"):
        monkeypatch.setattr(recovery, "_verify_batch", lambda *a: pytest.fail("untrusted bytes were read"))
    assert recovery.recover_upload_receipt(path, settings=get_settings())["state"] == "needs_attention"
    with SessionLocal() as db:
        assert db.scalars(select(ImportJob)).all() == []
    assert Path(receipt.get("source_path", "")).exists() if mutation != "source_path" else path.exists()


def test_legacy_receipt_recovers_with_confirmation_not_todays_auto_approval(recoverable, monkeypatch):
    path, receipt, _ = recoverable()
    receipt["schema_version"] = 1
    for field in ("job", "notifications", "printed_at_hint"):
        receipt.pop(field)
    replace_receipt(path, receipt)
    monkeypatch.setattr(get_settings(), "require_operator_import_confirmation", False)
    assert recovery.recover_upload_receipt(path, settings=get_settings())["state"] == "registered"
    with SessionLocal() as db:
        assert db.get(ImportJob, receipt["import_job_id"]).status == ImportJobStatus.awaiting_operator_confirmation


def test_registration_does_not_hold_sql_during_sha(recoverable, monkeypatch):
    path, _, _ = recoverable()
    held = set()
    def checkout(connection, record, proxy):
        held.add(id(connection))
    def checkin(connection, record):
        held.discard(id(connection))
    verify = recovery._verify_batch
    def guarded(*args):
        assert not held
        return verify(*args)
    monkeypatch.setattr(recovery, "_verify_batch", guarded)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        assert recovery.recover_upload_receipt(path, settings=get_settings())["state"] == "registered"
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_recovery_outage_has_durable_backoff_and_retries_same_id(recoverable, monkeypatch):
    path, receipt, _ = recoverable()
    original_commit = Session.commit
    def fail(self):
        raise OperationalError("COMMIT", {}, RuntimeError("NAS unavailable"))
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail)
        assert recovery.recover_upload_receipt(path, settings=get_settings(), now=100)["state"] == "deferred"
    assert json.loads(path.read_text())["recovery"]["retry_after"] > 100
    assert recovery.recover_upload_receipt(path, settings=get_settings(), now=100)["state"] == "skipped"
    assert recovery.recover_upload_receipt(path, settings=get_settings(), now=10000)["state"] == "registered"
    with SessionLocal() as db:
        assert db.get(ImportJob, receipt["import_job_id"]) is not None
    assert Session.commit == original_commit


def test_recovery_unknown_commit_does_not_duplicate_after_restart(recoverable, monkeypatch):
    path, receipt, _ = recoverable()
    original_commit = Session.commit
    def uncertain(self):
        original_commit(self)
        raise OperationalError("COMMIT", {}, RuntimeError("ACK lost"))
    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", uncertain)
        assert recovery.recover_upload_receipt(path, settings=get_settings(), now=100)["state"] == "deferred"
    assert recovery.recover_upload_receipt(path, settings=get_settings(), now=10000)["state"] == "registered"
    with SessionLocal() as db:
        assert [row.import_job_id for row in db.scalars(select(ImportJob))] == [receipt["import_job_id"]]


def test_scan_only_recovers_direct_private_receipts(recoverable, tmp_path):
    path, receipt, _ = recoverable(with_card=False)
    nested = tmp_path / "incoming" / "ordinary-folder"
    nested.mkdir()
    (nested / path.name).write_text(path.read_text())
    (tmp_path / "incoming" / "ordinary.json").write_text(path.read_text())
    results = recovery.recover_upload_receipts(settings=get_settings())
    assert len(results) == 1
    assert results[0]["state"] == "registered"
    assert recovery.recover_upload_receipts(settings=get_settings()) == []
    assert (nested / path.name).exists()


def test_recovery_can_win_before_original_http_commit_without_overwriting_worker(recoverable, monkeypatch):
    from domain.services.importing import uploads

    settings = get_settings()
    with session_scope() as db:
        card = PrintsRepository(db).create_print_record({"name": "Concurrent",
                                                        "origin_compute_node_id": settings.compute_node_id})
    original_write = uploads._write_receipt
    def recover_immediately(path, receipt):
        original_write(path, receipt)
        assert recovery.recover_upload_receipt(path, settings=settings)["state"] == "registered"
        with session_scope() as db:
            job = db.get(ImportJob, receipt["import_job_id"])
            job.status = ImportJobStatus.importing
            job.lease_owner = "worker-won"
            job.lease_generation = 3
            job.lease_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    monkeypatch.setattr(uploads, "_write_receipt", recover_immediately)
    with SessionLocal() as db:
        result = upload_log_batch(PrintsRepository(db), [LogUpload("17.07.2026.log", io.BytesIO(b"racing"))],
                                  settings=settings, record_id=card["record_id"])
    with SessionLocal() as db:
        jobs = db.scalars(select(ImportJob)).all()
        assert len(jobs) == 1
        assert jobs[0].lease_owner == "worker-won"
        assert jobs[0].lease_generation == 3
        assert jobs[0].status == ImportJobStatus.importing
        assert db.get(PrintRecord, card["record_id"]).revision == card["revision"] + 1
    assert result["jobs"][0]["lease_generation"] == 3


def test_recovery_preserves_date_changed_after_uncertain_upload(recoverable):
    path, _, record_id = recoverable()
    with session_scope() as db:
        card = db.get(PrintRecord, record_id)
        card.printed_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        card.metadata_json = {"new": "operator input"}
    assert recovery.recover_upload_receipt(path, settings=get_settings())["state"] == "registered"
    with SessionLocal() as db:
        card = db.get(PrintRecord, record_id)
        assert card.printed_at.date().isoformat() == "2026-08-01"
        assert card.metadata_json["new"] == "operator input"
