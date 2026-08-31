from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from api.routes.prints import _prepare_prediction_inputs
from api.routes.sessions import _generate_report, _report_for_read
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession, ReportArtifact
from domain.services.compute_affinity import ComputeAffinityError
from domain.services.import_jobs import _ensure_session_record
from storage.db.session import SessionLocal
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.runtime import RuntimeRepository


def _settings(node_id: str):
    return SimpleNamespace(compute_node_id=node_id)


def test_pc2_cannot_prepare_pc1_print_estimate(monkeypatch):
    with SessionLocal() as db:
        db.add(PrintRecord(
            record_id="pr_owned_by_pc1",
            origin_compute_node_id="pc-1",
            name="чужая карточка",
        ))
        db.flush()

        monkeypatch.setattr("api.routes.prints.get_settings", lambda: _settings("pc-2"))
        with pytest.raises(HTTPException) as caught:
            _prepare_prediction_inputs(PrintsRepository(db), "pr_owned_by_pc1")

    assert caught.value.status_code == 403


def test_pc2_cannot_reanalyze_pc1_session(monkeypatch):
    with SessionLocal() as db:
        db.add(BuildSession(
            session_id="session_owned_by_pc1",
            origin_compute_node_id="pc-1",
            context={"runtime_payload": {"files": [], "group": {}}},
        ))
        db.flush()

        monkeypatch.setattr("api.routes.sessions.get_settings", lambda: _settings("pc-2"))
        with pytest.raises(HTTPException) as caught:
            _generate_report(
                "session_owned_by_pc1",
                include_markdown=False,
                repo=RuntimeRepository(db),
            )

    assert caught.value.status_code == 403


def test_foreign_shared_get_uses_saved_report_without_rehydrate(monkeypatch):
    saved = {
        "report_id": "report_pc1",
        "session_id": "session_pc1_saved",
        "timeline": [{"event": "saved"}],
    }
    with SessionLocal() as db:
        db.add(BuildSession(
            session_id="session_pc1_saved",
            origin_compute_node_id="pc-1",
            context={"runtime_payload": {"files": [], "group": {}}},
        ))
        db.add(ReportArtifact(
            report_id="report_pc1",
            session_id="session_pc1_saved",
            report_type="session",
            payload=saved,
        ))
        db.flush()

        monkeypatch.setattr("api.routes.sessions.get_settings", lambda: _settings("pc-2"))
        monkeypatch.setattr(
            RuntimeRepository,
            "get_session_files",
            lambda *args, **kwargs: pytest.fail("foreign GET attempted raw rehydration"),
        )
        assert _report_for_read("session_pc1_saved", RuntimeRepository(db)) == saved


def test_foreign_shared_get_without_saved_report_is_conflict(monkeypatch):
    with SessionLocal() as db:
        db.add(BuildSession(
            session_id="session_pc1_no_report",
            origin_compute_node_id="pc-1",
            context={"runtime_payload": {"files": [], "group": {}}},
        ))
        db.flush()
        monkeypatch.setattr("api.routes.sessions.get_settings", lambda: _settings("pc-2"))

        with pytest.raises(HTTPException) as caught:
            _report_for_read("session_pc1_no_report", RuntimeRepository(db))

    assert caught.value.status_code == 409


def test_existing_foreign_session_cannot_be_overwritten_or_reused():
    with SessionLocal() as db:
        db.add(BuildSession(
            session_id="deterministic_collision",
            origin_compute_node_id="pc-1",
            context={"runtime_payload": {"files": [], "group": {}}},
        ))
        db.commit()

    with SessionLocal() as db:
        with pytest.raises(ComputeAffinityError):
            RuntimeRepository(db).save_session_payload(
                "deterministic_collision",
                {"files": [], "group": {}},
                origin_compute_node_id="pc-2",
            )

    with pytest.raises(ComputeAffinityError):
        _ensure_session_record(
            "deterministic_collision",
            1.0,
            origin_compute_node_id="pc-2",
        )


def test_session_link_is_owner_scoped_and_one_to_one():
    with SessionLocal() as db:
        db.add_all([
            BuildSession(session_id="session_pc1", origin_compute_node_id="pc-1"),
            BuildSession(session_id="session_pc2", origin_compute_node_id="pc-2"),
            PrintRecord(record_id="pr_pc1_a", origin_compute_node_id="pc-1", name="A"),
            PrintRecord(record_id="pr_pc1_b", origin_compute_node_id="pc-1", name="B"),
        ])
        db.flush()
        repo = PrintsRepository(db)

        assert not repo.link_session(
            "pr_pc1_a", "session_pc2", compute_node_id="pc-1"
        )
        assert repo.link_session(
            "pr_pc1_a", "session_pc1", compute_node_id="pc-1"
        )
        assert not repo.link_session(
            "pr_pc1_b", "session_pc1", compute_node_id="pc-1"
        )
        assert db.get(PrintRecord, "pr_pc1_a").session_id == "session_pc1"
        assert db.get(PrintRecord, "pr_pc1_b").session_id is None
