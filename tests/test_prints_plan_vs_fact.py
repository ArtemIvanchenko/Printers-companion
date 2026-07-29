"""The prints list must show plan and outcome side by side.

The prediction lives in the record's own snapshot, the outcome lives on the
linked log session, and until now nothing joined them — so "did the estimate
hold?" could not be answered from the list at all.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from api.main import app
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal

client = TestClient(app)


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session
        session.rollback()


def _session(db, session_id: str, *, machine_min=None, duration_min=None,
             idle_min=None, layers=174, classification="REAL_PRINT"):
    db.add(BuildSession(
        session_id=session_id,
        start_ts=datetime(2026, 3, 27, 10, tzinfo=timezone.utc),
        classification=classification,
        context={"runtime_payload": {"group": {
            "classification": classification,
            "features": {
                "machine_min": machine_min, "duration_min": duration_min,
                "idle_min": idle_min, "layers": layers,
            },
        }}},
    ))
    db.commit()


def _record(db, record_id: str, *, session_id=None, predicted_hours=None, cost=None):
    metadata = {}
    if predicted_hours is not None:
        metadata["prediction"] = {"print_hours": predicted_hours, "cost_total_rub": cost}
    db.add(PrintRecord(
        record_id=record_id, name=f"печать {record_id}", material="alsi10mg",
        session_id=session_id, metadata_json=metadata,
    ))
    db.commit()


def _summary(record_id: str) -> dict:
    items = client.get("/prints").json()["items"]
    return next(r["summary"] for r in items if r["record_id"] == record_id)


class TestPlanVsFactSummary:
    def test_machine_time_is_the_actual_not_the_wall_span(self, db):
        """The estimate models machine time, so the comparison must use it.

        A 4.1 h estimate against 4.4 h of machine time is a 6.8% miss; against
        the 8.2 h wall span (3.8 h of it operator pause) it would read as -50%,
        blaming the geometry for a coffee break.
        """
        _session(db, "s_mt", machine_min=264.0, duration_min=492.0, idle_min=228.0)
        _record(db, "pr_mt", session_id="s_mt", predicted_hours=4.1)

        summary = _summary("pr_mt")
        assert summary["actual_hours"] == pytest.approx(4.4)
        assert summary["actual_source"] == "machine_log"
        assert summary["error_pct"] == pytest.approx(-6.8, abs=0.1)
        assert summary["idle_hours"] == pytest.approx(3.8)

    def test_wall_span_used_only_when_no_machine_time(self, db):
        _session(db, "s_ws", duration_min=264.0)
        _record(db, "pr_ws", session_id="s_ws", predicted_hours=4.1)

        summary = _summary("pr_ws")
        assert summary["actual_source"] == "wall_span"
        assert summary["actual_hours"] == pytest.approx(4.4)

    def test_unlinked_record_reports_plan_without_fact(self, db):
        _record(db, "pr_plan", predicted_hours=4.1, cost=12400)

        summary = _summary("pr_plan")
        assert summary["predicted_hours"] == pytest.approx(4.1)
        assert summary["predicted_cost_rub"] == 12400
        assert summary["actual_hours"] is None
        assert summary["error_pct"] is None

    def test_linked_record_without_estimate_reports_fact_only(self, db):
        _session(db, "s_only", machine_min=264.0, layers=174)
        _record(db, "pr_only", session_id="s_only")

        summary = _summary("pr_only")
        assert summary["predicted_hours"] is None
        assert summary["actual_hours"] == pytest.approx(4.4)
        assert summary["layers"] == 174
        assert summary["error_pct"] is None


class TestUnlinkedSessions:
    def test_lists_sessions_with_no_card(self, db):
        _session(db, "s_free", machine_min=264.0)

        body = client.get("/prints/unlinked-sessions").json()
        assert [s["session_id"] for s in body["items"]] == ["s_free"]
        assert body["total"] == 1 and body["n_prints"] == 1

    def test_linked_session_disappears_from_the_list(self, db):
        _session(db, "s_taken", machine_min=264.0)
        _record(db, "pr_taken", session_id="s_taken")

        assert client.get("/prints/unlinked-sessions").json()["items"] == []

    def test_preparation_runs_are_listed_but_marked(self, db):
        """Linking one to a print card is rarely right — the operator must see it."""
        _session(db, "s_prep", duration_min=336.0, layers=0,
                 classification="PRE_BURN_SESSION")

        body = client.get("/prints/unlinked-sessions").json()
        assert body["total"] == 1
        assert body["n_prints"] == 0
        assert body["items"][0]["is_print"] is False

    def test_real_prints_sort_ahead_of_preparation_runs(self, db):
        _session(db, "s_prep2", duration_min=1.0, classification="PRE_BURN_SESSION")
        _session(db, "s_real2", machine_min=264.0, classification="REAL_PRINT")

        order = [s["session_id"] for s in client.get("/prints/unlinked-sessions").json()["items"]]
        assert order == ["s_real2", "s_prep2"]
