"""Strict QC lineage ordering, scope and ambiguity through real consumers."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from api.main import app
from api.routes.analysis import _load_quality_labels
from analytics.prediction.retraining import (
    _latest_label_evidence, enqueue_retraining_for_session, prepare_retraining,
)
from core.versioning.constants import ANALYSIS_VERSION
from core.versioning.provenance import stable_hash
from domain.models.jobs import BackgroundJob
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession
from domain.services import quality as producer
from domain.services.operator_report import build_operator_report
from domain.services.quality import final_outcome_head, final_outcome_heads
from storage.db.session import session_scope
from storage.repositories.prints_repo import PrintsRepository
from storage.repositories.runtime import RuntimeRepository

client = TestClient(app)
NOW = datetime(2026, 10, 14, 10, tzinfo=timezone.utc)


def row(identity, parent=None, *, card="card", session="session", result="accepted"):
    return {"outcome_id": identity, "supersedes_outcome_id": parent,
            "is_final": True, "print_record_id": card, "session_id": session,
            "timestamp": NOW, "result": result}


@pytest.mark.parametrize("history", [
    [row("a"), row("b")],
    [row("a"), row("b", "a"), row("c", "a")],
    [row("a", "missing")], [row("a", "a")],
    [row("a", "b"), row("b", "a")],
    [row("valid"), row("a", "b"), row("b", "a")],
    [row("a"), row("a")],
])
def test_unverifiable_full_lineage_never_selects_a_terminal(history):
    before = deepcopy(history)
    assert final_outcome_head(history) is None
    assert final_outcome_heads(history) == []
    assert history == before


def test_head_is_causal_not_timestamp_and_generic_rows_are_not_final():
    old, corrected = row("old"), row("corrected", "old", result="rejected")
    corrected["timestamp"] = NOW - timedelta(minutes=5)
    generic = {**row("generic", "corrected"), "is_final": False}
    assert final_outcome_head([corrected, generic, old]) == corrected
    assert final_outcome_heads([corrected, generic, old]) == [corrected]


def test_cardless_legacy_requires_one_complete_chain_not_a_timestamp_guess():
    assert final_outcome_heads([row("one", card=None)]) == [row("one", card=None)]
    assert final_outcome_heads([row("a", card=None), row("b", card=None)]) == []
    assert final_outcome_heads([row("a", card=None), row("b", "a", card=None)]) == [row("b", "a", card=None)]


def test_cross_scope_reference_and_unknown_competitor_poison_both_session_truths():
    cross = [row("a", card="A"), row("b", "a", card="B")]
    assert final_outcome_heads(cross) == []
    invalid = [row("a", card="A"), row("b", card="A")]
    valid = [row("valid", card="B")]
    assert final_outcome_heads(invalid + valid) == []
    # Existing API-only current-card fallback also cannot hide unknown scope.
    missing = [{**item, "session_id": None} for item in invalid]
    assert final_outcome_heads(missing + valid, card_sessions={"A": "session"}) == []


def group():
    return {"classification": "REAL_PRINT", "features": {"layers": 10},
            "analysis_snapshot": {"schema_version": 1,
                "analysis_id": "qc-chain-analysis", "features": {"layers": 10},
                "health": {}, "data_quality": {}, "signal_stats": {},
                "provenance": {"analysis_version": ANALYSIS_VERSION}}}


def create_case():
    sid = "qc-chain-" + uuid4().hex
    with session_scope() as db:
        db.add(BuildSession(session_id=sid, origin_compute_node_id="physical-owner-pc",
            classification="REAL_PRINT", start_ts=NOW - timedelta(days=1),
            context={"runtime_payload": {"group": group()}}))
        db.flush()
        card = PrintsRepository(db).create_print_record({"name": "QC lineage fixture",
            "material": "steel", "session_id": sid, "origin_compute_node_id": "physical-owner-pc"})
    return card, sid


def post_at(monkeypatch, card, timestamp, *, parent=None, result="accepted"):
    class ServerClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return timestamp if tz else timestamp.replace(tzinfo=None)
    payload = {"result": result, "inspection_result": "Synthetic inspection"}
    if parent:
        payload["supersedes_outcome_id"] = parent
    if result == "rejected":
        payload.update(defect_type="porosity", defect_location="Synthetic center")
    with monkeypatch.context() as patch:
        patch.setattr(producer, "datetime", ServerClock)
        return client.post(f"/prints/{card['record_id']}/quality-outcomes", json=payload,
                           headers={"X-Workstation-ID": "another-inspector-pc"})


@pytest.mark.parametrize("skew", [False, True])
def test_http_correction_preserves_owner_revision_and_causal_ml_head(monkeypatch, skew):
    card, sid = create_case()
    first = post_at(monkeypatch, card, NOW).json()
    correction = post_at(monkeypatch, card, NOW + timedelta(minutes=-5 if skew else 5),
                         parent=first["outcome_id"], result="rejected")
    assert correction.status_code == 200, correction.text
    head = correction.json()
    with session_scope() as db:
        prepared = prepare_retraining(db)["rows"]
        selected = next(item for item in prepared if item["session_id"] == sid)
        assert selected["label"] == 1 and selected["label_outcome_id"] == head["outcome_id"]
        assert datetime.fromisoformat(selected["label_observed_at"]) == datetime.fromisoformat(head["timestamp"])
        assert _load_quality_labels(db, session_id=sid) == {sid: 1}
        assert PrintsRepository(db).get_print_record(card["record_id"])["revision"] == card["revision"] + 2
        assert stable_hash(db.get(BuildSession, sid).context["runtime_payload"]["group"]) == stable_hash(group())
        jobs = db.scalars(select(BackgroundJob)).all()
        assert len(jobs) == 2 and all(job.owner_node_id == "physical-owner-pc" for job in jobs)
        enqueued = enqueue_retraining_for_session(db, sid)
        assert enqueued["payload"]["trigger_outcome_id"] == head["outcome_id"]
    history = client.get(f"/prints/{card['record_id']}/quality-outcomes").json()
    assert len(history) == 2 and history[0]["outcome_id"] == head["outcome_id"]
    report = client.get(f"/prints/{card['record_id']}/operator-report").json()
    assert report["quality_outcome"]["result"] == "rejected"
    fork = post_at(monkeypatch, card, NOW + timedelta(minutes=6), parent=first["outcome_id"])
    assert fork.status_code == 409
    continuation = post_at(monkeypatch, card, NOW + timedelta(minutes=6), parent=head["outcome_id"])
    assert continuation.status_code == 200, continuation.text


def test_historical_session_does_not_resurrect_superseded_card_ancestor():
    card, current_sid = create_case()
    old_sid = "old-" + uuid4().hex
    with session_scope() as db:
        db.add(BuildSession(session_id=old_sid, origin_compute_node_id="physical-owner-pc",
            classification="REAL_PRINT", context={"runtime_payload": {"group": group()}}))
        db.flush()
        first = QualityOutcome(outcome_id="old-head", print_record_id=card["record_id"],
            session_id=old_sid, timestamp=NOW, inspection_type="visual", result="accepted", is_final=True)
        corrected = QualityOutcome(outcome_id="new-head", print_record_id=card["record_id"],
            session_id=current_sid, timestamp=NOW - timedelta(minutes=5), inspection_type="ct",
            result="rejected", is_final=True, supersedes_outcome_id="old-head")
        db.add_all([first, corrected])
        db.flush()
        assert _load_quality_labels(db, session_id=old_sid) == {}
        assert _load_quality_labels(db, session_id=current_sid) == {current_sid: 1}
        assert old_sid not in _latest_label_evidence(db)
        assert enqueue_retraining_for_session(db, old_sid) is None
        history = RuntimeRepository(db).list_quality_outcomes(session_id=old_sid)
        assert {item["outcome_id"] for item in history} == {"old-head", "new-head"}
        assert build_operator_report(session_id=old_sid, group=group(), quality_outcomes=history)["quality_outcome"] is None
        assert build_operator_report(session_id=current_sid, group=group(), quality_outcomes=history)["quality_outcome"]["result"] == "rejected"
        # Exact physical card QC is not lost merely because its logs changed.
        historical = PrintsRepository(db).get_print_record(card["record_id"])
        historical["session_id"] = old_sid
        assert build_operator_report(session_id=old_sid, group=group(), quality_outcomes=history,
            print_record=historical)["quality_outcome"]["result"] == "rejected"


def test_api_current_card_link_fallback_preserves_only_missing_session():
    card, sid = create_case()
    with session_scope() as db:
        db.add(QualityOutcome(outcome_id="pre-link", print_record_id=card["record_id"],
            session_id=None, timestamp=NOW, inspection_type="visual", result="accepted", is_final=True))
        db.flush()
        assert _load_quality_labels(db, session_id=sid) == {sid: 0}
        # This API capability is not silently broadened into ML preparation.
        assert sid not in _latest_label_evidence(db)


def test_unknown_competing_stored_chain_cannot_become_unique_session_label(monkeypatch):
    card, sid = create_case()
    with session_scope() as db:
        other = PrintsRepository(db).create_print_record({"name": "Legacy other card",
            "origin_compute_node_id": "physical-owner-pc"})
        for identity, card_id in (("invalid-a", card["record_id"]), ("invalid-b", card["record_id"]),
                                  ("valid-other", other["record_id"])):
            db.add(QualityOutcome(outcome_id=identity, print_record_id=card_id, session_id=sid,
                timestamp=NOW, inspection_type="visual", result="accepted", is_final=True))
        db.flush()
        assert _load_quality_labels(db, session_id=sid) == {}
        assert sid not in _latest_label_evidence(db)
        assert enqueue_retraining_for_session(db, sid) is None
        assert len(RuntimeRepository(db).list_quality_outcomes(session_id=sid)) == 3
    refused = post_at(monkeypatch, card, NOW)
    assert refused.status_code == 409


def test_singleton_report_only_legacy_is_presentation_not_strict_ground_truth():
    legacy = {"result": "accepted"}
    assert final_outcome_heads([legacy]) == []
    assert build_operator_report(session_id="s", group=group(), quality_outcomes=[legacy])["quality_outcome"]["result"] == "accepted"
    assert build_operator_report(session_id="s", group=group(), quality_outcomes=[legacy, {"result": "rejected"}])["quality_outcome"] is None
    assert build_operator_report(session_id="s", group=group(), quality_outcomes=[{**legacy, "is_final": True}])["quality_outcome"] is None
