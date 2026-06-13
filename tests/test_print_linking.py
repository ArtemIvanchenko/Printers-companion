"""Tests for Phase 2: auto-linking sessions to print records + log import."""
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from domain.services.print_linking import auto_link_print_records
from storage.db.session import SessionLocal

client = TestClient(app)


def _make_session(db, session_id: str, start: datetime) -> None:
    db.add(BuildSession(session_id=session_id, status="runtime_payload", context={}, start_ts=start))


def _make_record(db, record_id: str, name: str, printed_at: datetime | None) -> None:
    db.add(PrintRecord(record_id=record_id, name=name, printed_at=printed_at))


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


class TestAutoLink:
    def test_links_unambiguous_pair(self, db):
        ts = datetime(2027, 1, 10, 9, 0, tzinfo=timezone.utc)
        _make_session(db, "s_link1", ts)
        _make_record(db, "pr_link1", "деталь", ts.replace(hour=2))
        db.flush()

        links = auto_link_print_records(db)
        assert {"record_id": "pr_link1", "session_id": "s_link1"} in links
        record = db.get(PrintRecord, "pr_link1")
        assert record.session_id == "s_link1"
        # Дата печати перезаписана временем старта из логов
        assert record.printed_at.replace(tzinfo=timezone.utc) == ts

    def test_skips_when_record_matches_two_sessions(self, db):
        ts = datetime(2027, 2, 10, 9, 0, tzinfo=timezone.utc)
        _make_session(db, "s_amb1", ts)
        _make_session(db, "s_amb2", ts.replace(hour=12))
        _make_record(db, "pr_amb", "деталь", ts)
        db.flush()

        links = auto_link_print_records(db)
        assert all(l["record_id"] != "pr_amb" for l in links)
        assert db.get(PrintRecord, "pr_amb").session_id is None

    def test_skips_when_session_matches_two_records(self, db):
        ts = datetime(2027, 3, 10, 9, 0, tzinfo=timezone.utc)
        _make_session(db, "s_two_rec", ts)
        _make_record(db, "pr_a", "деталь А", ts)
        _make_record(db, "pr_b", "деталь Б", ts.replace(hour=11))
        db.flush()

        links = auto_link_print_records(db)
        linked = {l["session_id"] for l in links}
        assert "s_two_rec" not in linked

    def test_outside_window_not_linked(self, db):
        ts = datetime(2027, 4, 10, 9, 0, tzinfo=timezone.utc)
        _make_session(db, "s_far", ts)
        _make_record(db, "pr_far", "деталь", datetime(2027, 4, 20, tzinfo=timezone.utc))
        db.flush()

        auto_link_print_records(db)
        assert db.get(PrintRecord, "pr_far").session_id is None

    def test_already_taken_session_not_relinked(self, db):
        ts = datetime(2027, 5, 10, 9, 0, tzinfo=timezone.utc)
        _make_session(db, "s_taken", ts)
        db.add(PrintRecord(record_id="pr_owner", name="владелец", session_id="s_taken", printed_at=ts))
        _make_record(db, "pr_late", "опоздавшая", ts)
        db.flush()

        auto_link_print_records(db)
        assert db.get(PrintRecord, "pr_late").session_id is None

    def test_idempotent(self, db):
        ts = datetime(2027, 6, 10, 9, 0, tzinfo=timezone.utc)
        _make_session(db, "s_idem", ts)
        _make_record(db, "pr_idem", "деталь", ts)
        db.flush()

        first = auto_link_print_records(db)
        second = auto_link_print_records(db)
        assert len([l for l in first if l["record_id"] == "pr_idem"]) == 1
        assert not [l for l in second if l["record_id"] == "pr_idem"]


class TestImportHint:
    def test_hint_beats_record_ambiguity(self, db):
        """Два рекорда на одну дату, но у одного есть hint от import-logs."""
        ts = datetime(2027, 7, 10, 9, 0, tzinfo=timezone.utc)
        _make_session(db, "s_hint", ts)
        db.add(PrintRecord(record_id="pr_hinted", name="с хинтом", printed_at=ts,
                           metadata_json={"log_import_hint": {"date": "2027-07-10"}}))
        _make_record(db, "pr_rival", "соперник", ts.replace(hour=11))
        db.flush()

        links = auto_link_print_records(db)
        assert {"record_id": "pr_hinted", "session_id": "s_hint"} in links
        hinted = db.get(PrintRecord, "pr_hinted")
        assert hinted.session_id == "s_hint"
        assert "log_import_hint" not in (hinted.metadata_json or {})
        assert db.get(PrintRecord, "pr_rival").session_id is None

    def test_hint_kept_until_session_appears(self, db):
        db.add(PrintRecord(record_id="pr_waiting", name="ждёт", printed_at=None,
                           metadata_json={"log_import_hint": {"date": "2027-08-15"}}))
        # сессия нужна хотя бы одна, иначе ранний return
        _make_session(db, "s_other_date", datetime(2027, 9, 1, tzinfo=timezone.utc))
        db.flush()

        auto_link_print_records(db)
        waiting = db.get(PrintRecord, "pr_waiting")
        assert waiting.session_id is None
        assert waiting.metadata_json.get("log_import_hint")  # хинт не потерян


class TestSessionCandidatesEndpoint:
    def test_candidates_listed_including_ambiguous(self):
        ts = datetime(2027, 10, 5, 9, 0, tzinfo=timezone.utc)
        with SessionLocal() as db:
            _make_session(db, "s_cand_a", ts)
            _make_session(db, "s_cand_b", ts.replace(hour=15))
            db.commit()
        rec = client.post("/prints", json={"name": "x", "printed_at": "2027-10-05"}).json()
        r = client.get(f"/prints/{rec['record_id']}/session-candidates")
        assert r.status_code == 200
        ids = {c["session_id"] for c in r.json()["candidates"]}
        assert {"s_cand_a", "s_cand_b"} <= ids

    def test_missing_record_404(self):
        assert client.get("/prints/pr_nope/session-candidates").status_code == 404


class TestImportLogsEndpoint:
    def test_upload_logs_saves_and_sets_date(self, tmp_path, monkeypatch):
        from core.config.settings import get_settings

        monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(tmp_path))

        record = client.post("/prints", json={"name": "печать без даты"}).json()
        assert record["printed_at"] is None

        r = client.post(
            f"/prints/{record['record_id']}/import-logs",
            files=[("files", ("23.05.2027.log", io.BytesIO(b"log data"), "text/plain"))],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["saved"][0]["name"] == "23.05.2027.log"
        assert (tmp_path / "23.05.2027.log").read_bytes() == b"log data"

        updated = client.get(f"/prints/{record['record_id']}").json()
        assert updated["printed_at"].startswith("2027-05-23")

    def test_upload_rejects_wrong_suffix(self, tmp_path, monkeypatch):
        from core.config.settings import get_settings

        monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(tmp_path))
        record = client.post("/prints", json={"name": "x"}).json()
        r = client.post(
            f"/prints/{record['record_id']}/import-logs",
            files=[("files", ("virus.exe", io.BytesIO(b"x"), "application/octet-stream"))],
        )
        assert r.status_code == 200
        assert r.json()["saved"] == []
        assert r.json()["skipped"][0]["reason"] == "неподдерживаемый тип файла"

    def test_missing_record_404(self, tmp_path, monkeypatch):
        from core.config.settings import get_settings

        monkeypatch.setattr(get_settings(), "raw_logs_container_path", str(tmp_path))
        r = client.post(
            "/prints/pr_missing/import-logs",
            files=[("files", ("a.log", io.BytesIO(b"x"), "text/plain"))],
        )
        assert r.status_code == 404
