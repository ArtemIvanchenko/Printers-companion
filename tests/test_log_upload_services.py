"""Log upload contracts, portable file names and publication race regressions."""

import asyncio
import hashlib
import io
import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile

from api.main import app
from api.routes import prints
from core.config.settings import get_settings
from domain.models.prints import PrintRecord
from domain.models.sessions import ImportJob
from storage.db.session import SessionLocal, engine, session_scope
from storage.repositories.prints_repo import PrintsRepository


client = TestClient(app)
SAVED_KEYS = {"name", "stored_name", "size_bytes", "checksum", "duplicate"}
CARD_NOTE = (
    "Подтвердите импорт в верхней панели; одна сессия из этого задания привяжется "
    "к карточке. Если сессий несколько, потребуется выбор."
)


@pytest.fixture
def upload_root(tmp_path, monkeypatch):
    root = tmp_path / "raw-logs"
    root.mkdir()
    settings = get_settings()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(root))
    monkeypatch.setattr(settings, "compute_node_id", "upload-owner")
    monkeypatch.setattr(settings, "require_operator_import_confirmation", True)
    return root


def make_card():
    with session_scope() as db:
        return PrintsRepository(db).create_print_record(
            {
                "name": "Логи печати",
                "origin_compute_node_id": "upload-owner",
                "metadata_json": {"retained": "before"},
            }
        )


def endpoint(card_specific):
    if card_specific:
        card = make_card()
        return f"/prints/{card['record_id']}/import-logs", card["record_id"]
    return "/upload/logs", None


def post_files(url, items):
    return client.post(url, files=[("files", (name, data, "text/plain")) for name, data in items])


def assert_no_batch_or_job(root):
    incoming = root / "incoming"
    assert not incoming.exists() or not list(incoming.iterdir())
    with SessionLocal() as db:
        assert db.scalars(select(ImportJob)).all() == []


class CallbackStream(io.BytesIO):
    """Perform a concurrent change after upload validation but before copying."""

    def __init__(self, callback, data=b"machine log"):
        super().__init__(data)
        self.callback = callback

    def read(self, size=-1):
        if self.callback is not None:
            callback, self.callback = self.callback, None
            callback()
        return super().read(size)


def direct_card_upload(record_id, callback):
    with SessionLocal() as db:
        result = asyncio.run(
            prints.import_logs_for_print(
                record_id,
                [UploadFile(file=CallbackStream(callback), filename="17.07.2026.log")],
                PrintsRepository(db),
            )
        )
        db.commit()
        return result


@pytest.mark.parametrize("card_specific", [False, True])
def test_upload_response_contract_and_owner_local_job(upload_root, card_specific):
    url, record_id = endpoint(card_specific)
    response = post_files(url, [("17.07.2026.log", b"main"), ("17.07.2026_time.log", b"time")])
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == (
        {"saved", "skipped", "jobs", "note"} if card_specific else {"saved", "skipped", "jobs"}
    )
    assert body["skipped"] == []
    assert len(body["saved"]) == 2
    assert all(set(saved) == SAVED_KEYS for saved in body["saved"])
    assert len(body["jobs"]) == 1
    job = body["jobs"][0]
    assert job["status"] == "awaiting_operator_confirmation"
    assert job["owner_node_id"] == "upload-owner"
    assert job["print_record_id"] == record_id
    with SessionLocal() as db:
        assert db.get(ImportJob, job["import_job_id"]) is not None
        if card_specific:
            card = db.get(PrintRecord, record_id)
            assert card.printed_at.date().isoformat() == "2026-07-17"
            assert card.metadata_json["retained"] == "before"
            assert card.metadata_json["log_import_hint"] == {"date": "2026-07-17"}
    if card_specific:
        assert body["note"] == CARD_NOTE


@pytest.mark.parametrize("card_specific", [False, True])
def test_same_name_deduplicates_identical_bytes_without_losing_different_bytes(
    upload_root, card_specific
):
    url, _ = endpoint(card_specific)
    response = post_files(url, [("a.log", b"first"), ("a.log", b"first"), ("a.log", b"second")])
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["duplicate"] for item in body["saved"]] == [False, True, False]
    assert body["saved"][0]["stored_name"] == body["saved"][1]["stored_name"]
    assert body["saved"][2]["stored_name"] != body["saved"][0]["stored_name"]
    batch = Path(body["jobs"][0]["source_path"])
    assert {file.read_bytes() for file in batch.iterdir()} == {b"first", b"second"}
    for saved in body["saved"]:
        data = (batch / saved["stored_name"]).read_bytes()
        assert saved["checksum"] == hashlib.sha256(data).hexdigest()
        assert saved["size_bytes"] == len(data)


@pytest.mark.parametrize("card_specific", [False, True])
@pytest.mark.parametrize("occupied_style", ["legacy_checksum", "copy_counter"])
def test_checksum_fallback_name_cannot_overwrite_existing_file(
    upload_root, card_specific, occupied_style
):
    url, _ = endpoint(card_specific)
    occupied = (
        f"a__{hashlib.sha256(b'B').hexdigest()[:12]}.log"
        if occupied_style == "legacy_checksum"
        else "copy_1__a.log"
    )
    response = post_files(url, [("a.log", b"A"), (occupied, b"C"), ("a.log", b"B")])
    assert response.status_code == 200, response.text
    body = response.json()
    batch = Path(body["jobs"][0]["source_path"])
    assert len({item["stored_name"] for item in body["saved"]}) == 3
    assert {file.read_bytes() for file in batch.iterdir()} == {b"A", b"B", b"C"}
    assert (batch / body["saved"][1]["stored_name"]).read_bytes() == b"C"


@pytest.mark.parametrize(
    "suffix", ["time", "sensors", "Monitor100", "Monitor200", "error", "stateFlow", "burn"]
)
def test_renaming_conflicting_logs_preserves_parser_family_and_date(upload_root, suffix):
    from domain.services.file_classifier import classify_file
    from parsers.common.timestamps import date_hint_from_filename

    original = f"17.07.2026_{suffix}.log"
    response = post_files("/upload/logs", [(original, b"first"), (original, b"second")])
    assert response.status_code == 200, response.text
    for saved in response.json()["saved"]:
        stored = Path(saved["stored_name"])
        assert classify_file(stored).family == classify_file(Path(original)).family
        assert date_hint_from_filename(stored) == date_hint_from_filename(Path(original))


def test_long_valid_name_keeps_parser_suffix_and_overlong_name_is_reported(upload_root):
    from domain.services.file_classifier import classify_file

    original = "a" * 181 + "_time.log"
    overlong = "b" * 256 + "_time.log"
    response = post_files(
        "/upload/logs", [(original, b"first"), (original, b"second"), (overlong, b"too long")]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["saved"]) == 2
    for saved in body["saved"]:
        assert (
            classify_file(Path(saved["stored_name"])).family == classify_file(Path(original)).family
        )
    assert body["skipped"] == [{"name": overlong, "reason": "слишком длинное имя файла"}]


@pytest.mark.parametrize("names", [("Straße.log", "STRASSE.log"), ("é.log", "e\u0301.log")])
def test_stored_names_are_unique_under_unicode_and_casefold(upload_root, names):
    response = post_files("/upload/logs", [(names[0], b"one"), (names[1], b"two")])
    assert response.status_code == 200, response.text
    body = response.json()
    portable = [
        unicodedata.normalize("NFC", item["stored_name"]).casefold() for item in body["saved"]
    ]
    assert len(set(portable)) == 2
    batch = Path(body["jobs"][0]["source_path"])
    assert {file.read_bytes() for file in batch.iterdir()} == {b"one", b"two"}


@pytest.mark.parametrize("card_specific", [False, True])
def test_all_unsupported_uploads_leave_no_batch_or_queue(upload_root, card_specific):
    url, _ = endpoint(card_specific)
    response = post_files(url, [("wrong.exe", b"not a log")])
    assert response.status_code == 200, response.text
    assert response.json()["saved"] == []
    assert response.json()["jobs"] == []
    assert response.json()["skipped"] == [
        {"name": "wrong.exe", "reason": "неподдерживаемый тип файла"}
    ]
    assert_no_batch_or_job(upload_root)


@pytest.mark.parametrize("card_specific", [False, True])
def test_all_oversized_uploads_leave_no_batch_or_queue(upload_root, monkeypatch, card_specific):
    monkeypatch.setattr("domain.services.importing.uploads.MAX_FILE_MB", 0)
    url, _ = endpoint(card_specific)
    response = post_files(url, [("too-big.log", b"x")])
    assert response.status_code == 200, response.text
    assert response.json()["saved"] == []
    assert response.json()["jobs"] == []
    assert response.json()["skipped"] == [{"name": "too-big.log", "reason": "файл > 0 МБ"}]
    assert_no_batch_or_job(upload_root)


def test_concurrent_date_and_metadata_edit_survive_upload(upload_root):
    card = make_card()
    operator_date = datetime(2026, 8, 9, tzinfo=timezone.utc)

    def edit_card():
        with session_scope() as other_db:
            PrintsRepository(other_db).update_print_record(
                card["record_id"],
                {
                    "printed_at": operator_date,
                    "metadata_json": {"retained": "after", "operator_comment": "checked"},
                },
            )

    body = direct_card_upload(card["record_id"], edit_card)
    assert len(body["jobs"]) == 1
    with SessionLocal() as db:
        current = db.get(PrintRecord, card["record_id"])
        assert current.printed_at.date() == operator_date.date()
        assert current.metadata_json["retained"] == "after"
        assert current.metadata_json["operator_comment"] == "checked"
        assert current.metadata_json["log_import_hint"] == {"date": "2026-07-17"}


@pytest.mark.parametrize("change,expected_status", [("delete", 404), ("owner", 403)])
def test_concurrent_delete_or_owner_change_rejects_publication(
    upload_root, change, expected_status
):
    card = make_card()

    def mutate_card():
        with session_scope() as other_db:
            row = other_db.get(PrintRecord, card["record_id"])
            if change == "delete":
                other_db.delete(row)
            else:
                row.origin_compute_node_id = "different-owner"

    with pytest.raises(HTTPException) as error:
        direct_card_upload(card["record_id"], mutate_card)
    assert error.value.status_code == expected_status
    assert_no_batch_or_job(upload_root)


def test_sql_connection_is_released_while_upload_is_read(upload_root):
    card = make_card()
    checked_out = set()
    reads = []

    def checkout(connection, _record, _proxy):
        checked_out.add(id(connection))

    def checkin(connection, _record):
        checked_out.discard(id(connection))

    def assert_released():
        reads.append(True)
        assert not checked_out, "upload copying must not retain a NAS SQL connection"

    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        body = direct_card_upload(card["record_id"], assert_released)
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert reads == [True]
    assert len(body["jobs"]) == 1


@pytest.mark.parametrize("card_specific", [False, True])
def test_commit_failure_keeps_private_recoverable_copy_without_publishing(
    upload_root, monkeypatch, card_specific
):
    url, record_id = endpoint(card_specific)

    def unavailable_commit(self):
        raise OperationalError("COMMIT", {}, RuntimeError("NAS unavailable"))

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", unavailable_commit)
        response = TestClient(app, raise_server_exceptions=False).post(
            url,
            files=[("files", ("17.07.2026.log", b"retain after failed commit", "text/plain"))],
        )
    assert response.status_code == 503, response.text
    with SessionLocal() as db:
        assert db.scalars(select(ImportJob)).all() == []
        if record_id is not None:
            card = db.get(PrintRecord, record_id)
            assert card.printed_at is None
            assert card.metadata_json == {"retained": "before"}
    entries = list((upload_root / "incoming").iterdir())
    batches = [entry for entry in entries if entry.is_dir()]
    receipts = [entry for entry in entries if entry.suffix == ".json"]
    assert len(batches) == len(receipts) == 1
    assert batches[0].name.startswith(".browser-upload-")
    assert (batches[0] / "17.07.2026.log").read_bytes() == b"retain after failed commit"
    assert isinstance(json.loads(receipts[0].read_text()), dict)


def test_windows_and_private_prefix_names_survive_worker_snapshot(upload_root):
    from domain.services.import_jobs import calculate_checksum_manifest, snapshot_source

    response = post_files(
        "/upload/logs",
        [
            (r"C:\PrinterLogs\17.07.2026.log", b"Windows export"),
            ("CON.log", b"reserved Windows name"),
            (".browser-upload-hidden.log", b"user supplied reserved prefix"),
        ],
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["saved"][0]["name"] == "17.07.2026.log"
    assert len(body["saved"]) == 3
    stored = [item["stored_name"] for item in body["saved"]]
    assert all("/" not in name and "\\" not in name for name in stored)
    assert all(Path(name).stem.upper() != "CON" for name in stored)
    assert all(not name.startswith(".browser-upload-") for name in stored)
    batch = Path(body["jobs"][0]["source_path"])
    snapshot = snapshot_source(batch)
    manifest = calculate_checksum_manifest(batch)
    assert set(snapshot) == set(manifest) == set(stored)
    expected_manifest = {item["stored_name"]: item["checksum"] for item in body["saved"]}
    assert manifest == expected_manifest == body["jobs"][0]["checksum_manifest"]
    for item in body["saved"]:
        assert snapshot[item["stored_name"]]["size"] == item["size_bytes"]
        assert (
            hashlib.sha256((batch / item["stored_name"]).read_bytes()).hexdigest()
            == item["checksum"]
        )


@pytest.mark.parametrize("card_specific", [False, True])
def test_missing_raw_directory_preserves_http_500_contract(upload_root, monkeypatch, card_specific):
    missing = upload_root / "missing"
    monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(missing))
    url, _ = endpoint(card_specific)
    response = post_files(url, [("a.log", b"log")])
    assert response.status_code == 500, response.text
    assert response.json()["detail"] == f"Папка логов не найдена: {missing}"
    assert not missing.exists()
    assert_no_batch_or_job(upload_root)


def test_snapshot_preparation_holds_no_sql_connection(upload_root, monkeypatch):
    from domain.services.importing import uploads

    card = make_card()
    checked_out = set()
    snapshots = []
    original_snapshot = uploads.snapshot_source

    def checkout(connection, _record, _proxy):
        checked_out.add(id(connection))

    def checkin(connection, _record):
        checked_out.discard(id(connection))

    def inspect_snapshot(source):
        assert not checked_out, "filesystem snapshot must precede SQL publication"
        snapshots.append(source)
        return original_snapshot(source)

    monkeypatch.setattr(uploads, "snapshot_source", inspect_snapshot)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        with SessionLocal() as db:
            body = uploads.upload_log_batch(
                PrintsRepository(db),
                [uploads.LogUpload("a.log", io.BytesIO(b"local snapshot"))],
                settings=get_settings(),
                record_id=card["record_id"],
            )
            assert not db.in_transaction()
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)
    assert snapshots == [Path(body["jobs"][0]["source_path"])]


def test_uncertain_commit_preserves_committed_job_inputs_and_receipt(upload_root, monkeypatch):
    url, record_id = endpoint(True)
    original_commit = Session.commit
    commit_calls = []

    def committed_but_acknowledgement_lost(self):
        original_commit(self)
        commit_calls.append(True)
        if len(commit_calls) == 1:
            raise OperationalError("COMMIT", {}, RuntimeError("commit acknowledgement lost"))

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", committed_but_acknowledgement_lost)
        response = TestClient(app, raise_server_exceptions=False).post(
            url,
            files=[("files", ("17.07.2026.log", b"already committed", "text/plain"))],
        )
    assert response.status_code == 503, response.text
    assert len(commit_calls) == 1
    with SessionLocal() as db:
        jobs = db.scalars(select(ImportJob)).all()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.print_record_id == record_id
        assert job.owner_node_id == "upload-owner"
        batch = Path(job.source_path)
        assert (batch / "17.07.2026.log").read_bytes() == b"already committed"
        receipt = json.loads(batch.with_name(batch.name + ".json").read_text())
        assert receipt["import_job_id"] == job.import_job_id
        assert receipt["source_path"] == job.source_path
        assert receipt["print_record_id"] == record_id
        assert receipt["owner_node_id"] == job.owner_node_id
        assert db.get(PrintRecord, record_id).printed_at.date().isoformat() == "2026-07-17"
