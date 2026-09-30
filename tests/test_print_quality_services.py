"""Print quality contracts and application transaction boundaries."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.requests import Request

from api.main import app
from api.routes import prints
from domain.models.jobs import BackgroundJob
from domain.models.prints import PrintRecord
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession
from domain.services.print_cards import quality
from domain.services.print_cards.contracts import CardError
from storage.db.session import SessionLocal, engine, session_scope
from storage.repositories.prints_repo import PrintsRepository


client = TestClient(app)
LABEL = {"result": "accepted", "inspection_result": "Размеры в допуске"}
OUTCOME_KEYS = {
    "outcome_id",
    "print_record_id",
    "session_id",
    "build_id",
    "part_id",
    "timestamp",
    "inspection_type",
    "result",
    "is_final",
    "supersedes_outcome_id",
    "inspection_result",
    "defect_type",
    "defect_location",
    "layer_range",
    "severity",
    "notes",
    "attachments",
    "created_by",
    "evidence_links",
}


def make_card(*, linked=False):
    with session_scope() as db:
        if linked:
            db.add(
                BuildSession(
                    session_id="quality-session",
                    origin_compute_node_id="owner-pc",
                    start_ts=datetime(2026, 9, 1, tzinfo=timezone.utc),
                    context={
                        "runtime_payload": {
                            "group": {
                                "classification": "REAL_PRINT",
                                "confidence": 0.9,
                                "data_quality": {"score": 95},
                                "features": {"layers": 10},
                            }
                        }
                    },
                )
            )
            db.flush()
        return PrintsRepository(db).create_print_record(
            {
                "name": "Контроль качества",
                "origin_compute_node_id": "owner-pc",
                "session_id": "quality-session" if linked else None,
            }
        )


def post_label(record_id, **kwargs):
    return client.post(f"/prints/{record_id}/quality-outcomes", json=LABEL, **kwargs)


@pytest.mark.parametrize("linked", [False, True])
def test_quality_http_response_contract_and_owner_affinity(linked):
    card = make_card(linked=linked)
    response = post_label(card["record_id"], headers={"X-Workstation-ID": "inspector-pc"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == OUTCOME_KEYS | {"model_retraining_job_id"}
    assert body["created_by"] == "inspector-pc"
    assert body["session_id"] == card["session_id"]
    assert body["is_final"] is True and body["result"] == "accepted"
    history = client.get(f"/prints/{card['record_id']}/quality-outcomes").json()
    assert len(history) == 1 and set(history[0]) == OUTCOME_KEYS
    assert history[0]["outcome_id"] == body["outcome_id"]
    with session_scope() as db:
        current = PrintsRepository(db).get_print_record(card["record_id"])
        assert current["revision"] == card["revision"] + 1
        jobs = db.scalars(select(BackgroundJob)).all()
        assert len(jobs) == int(linked)
        if linked:
            assert jobs[0].job_id == body["model_retraining_job_id"]
            assert jobs[0].owner_node_id == "owner-pc"
            assert jobs[0].payload_json["trigger_outcome_id"] == body["outcome_id"]
        else:
            assert body["model_retraining_job_id"] is None


@pytest.mark.parametrize("path", ["quality-outcomes", "operator-report"])
def test_missing_card_read_contract(path):
    response = client.get(f"/prints/pr_missing/{path}")
    assert response.status_code == 404
    assert response.json() == {"detail": "Карточка печати не найдена"}


def test_missing_card_write_contract():
    response = post_label("pr_missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "Карточка печати не найдена"}


@pytest.mark.parametrize("linked", [False, True])
def test_operator_report_response_contract(linked):
    card = make_card(linked=linked)
    response = client.get(f"/prints/{card['record_id']}/operator-report")
    assert response.status_code == 200, response.text
    report = response.json()
    assert set(report) == {
        "schema_version",
        "print_record_id",
        "print_name",
        "session_id",
        "classification",
        "state",
        "confidence",
        "deviations",
        "possible_causes",
        "recommendations",
        "quality_outcome",
        "disclaimer",
        "version_metadata",
    }
    assert report["schema_version"] == "operator-report-1.0"
    assert report["session_id"] == card["session_id"]
    assert report["print_record_id"] == card["record_id"]
    assert report["quality_outcome"] is None
    assert report["state"]["code"] == (
        "no_significant_deviations" if linked else "not_a_confirmed_print"
    )


def test_report_releases_sql_before_building(monkeypatch):
    card = make_card(linked=True)
    held, checked = set(), []

    def checkout(connection, record, proxy):
        held.add(id(connection))

    def checkin(connection, record):
        held.discard(id(connection))

    def build(**inputs):
        assert not held, "operator report holds a NAS SQL connection during calculation"
        assert inputs["group"]["features"]["layers"] == 10
        assert inputs["print_record"]["record_id"] == card["record_id"]
        checked.append(True)
        return {"detached": True}

    monkeypatch.setattr("domain.services.operator_report.build_operator_report", build)
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    try:
        response = client.get(f"/prints/{card['record_id']}/operator-report")
        assert response.json() == {"detached": True} and checked == [True]
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_report_cache_invalidation_only_observes_committed_label(monkeypatch):
    card = make_card(linked=True)
    observed = []

    def invalidate(session_id):
        with session_scope() as db:
            outcome = db.scalar(select(QualityOutcome))
            job = db.scalar(select(BackgroundJob))
            assert outcome is not None, "cache invalidated before the inspection committed"
            assert job is not None and job.payload_json["trigger_outcome_id"] == outcome.outcome_id
            assert db.get(PrintRecord, card["record_id"]).revision == card["revision"] + 1
        observed.append(session_id)

    monkeypatch.setattr("api.routes.sessions._invalidate_cache", invalidate)
    assert post_label(card["record_id"]).status_code == 200
    assert observed == [card["session_id"]]


def test_failed_commit_preserves_card_and_cache(monkeypatch):
    card = make_card(linked=True)
    invalidations = []
    monkeypatch.setattr("api.routes.sessions._invalidate_cache", invalidations.append)

    def fail_commit(self):
        raise OperationalError("COMMIT", {}, ConnectionError("NAS disconnected"))

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(OperationalError):
            post_label(card["record_id"])
    with session_scope() as db:
        assert db.scalar(select(QualityOutcome)) is None
        assert db.scalar(select(BackgroundJob)) is None
        assert db.get(PrintRecord, card["record_id"]).revision == card["revision"]
    assert invalidations == [], "failed publication must not invalidate the report cache"


def test_quality_uses_fresh_locked_card_link(monkeypatch):
    card = make_card(linked=True)
    monkeypatch.setattr("api.routes.sessions._invalidate_cache", lambda _: None)
    with SessionLocal() as db:
        stale = db.get(PrintRecord, card["record_id"])
        assert stale.session_id == "quality-session"
        # Keep an expired-in-the-world, but not expired-in-the-identity-map copy.
        # Another request changes the link before this request locks the card.
        db.commit()
        with session_scope() as other:
            other.add(
                BuildSession(session_id="replacement-session", origin_compute_node_id="owner-pc")
            )
            other.flush()
            other.get(PrintRecord, card["record_id"]).session_id = "replacement-session"
        request = Request({"type": "http", "headers": []})
        outcome = prints.create_print_quality_outcome(
            card["record_id"],
            LABEL,
            request,
            PrintsRepository(db),
        )
        assert outcome["session_id"] == "replacement-session"
        assert not db.in_transaction()
    with session_scope() as db:
        assert db.get(QualityOutcome, outcome["outcome_id"]).session_id == "replacement-session"
        job = db.get(BackgroundJob, outcome["model_retraining_job_id"])
        assert job.payload_json["trigger_session_id"] == "replacement-session"


@pytest.mark.parametrize("failure_point", ["label", "job", "revision"])
def test_publication_failure_rolls_back_every_write(monkeypatch, failure_point):
    from analytics.prediction import retraining
    from storage.repositories.runtime import RuntimeRepository

    card = make_card(linked=True)
    invalidations = []
    monkeypatch.setattr("api.routes.sessions._invalidate_cache", invalidations.append)
    owner, name = {
        "label": (RuntimeRepository, "save_quality_outcome"),
        "job": (retraining, "enqueue_retraining"),
        "revision": (PrintsRepository, "_touch_print_record"),
    }[failure_point]
    original = getattr(owner, name)

    def fail_after_write(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("publication interrupted")

    monkeypatch.setattr(owner, name, fail_after_write)
    with pytest.raises(RuntimeError, match="publication interrupted"):
        post_label(card["record_id"])
    with session_scope() as db:
        assert db.scalar(select(QualityOutcome)) is None
        assert db.scalar(select(BackgroundJob)) is None
        assert db.get(PrintRecord, card["record_id"]).revision == card["revision"]
    assert invalidations == []


def test_quality_services_work_without_http_or_outer_commit():
    card = make_card(linked=True)
    with SessionLocal() as db:
        repo = PrintsRepository(db)
        outcome = quality.create_quality_outcome(repo, card["record_id"], LABEL, actor="inspector")
        assert not db.in_transaction()
        # A separate reader sees all three writes before this session closes.
        with session_scope() as reader:
            assert reader.get(QualityOutcome, outcome["outcome_id"]).created_by == "inspector"
            assert reader.get(BackgroundJob, outcome["model_retraining_job_id"]) is not None
            assert reader.get(PrintRecord, card["record_id"]).revision == card["revision"] + 1
        history = quality.list_quality_outcomes(repo, card["record_id"])
        assert history[0]["outcome_id"] == outcome["outcome_id"]
        assert not db.in_transaction()
        report = quality.get_operator_report(repo, card["record_id"])
        assert report["quality_outcome"]["inspection_result"] == outcome["inspection_result"]
        assert report["quality_outcome"]["created_by"] == "inspector"
        assert report["state"]["code"] == "accepted"
        assert not db.in_transaction()


@pytest.mark.parametrize("read", [quality.list_quality_outcomes, quality.get_operator_report])
def test_missing_card_service_releases_transaction(read):
    with SessionLocal() as db:
        with pytest.raises(CardError) as error:
            read(PrintsRepository(db), "pr_missing")
        assert error.value.code == "not_found"
        assert not db.in_transaction()


@pytest.mark.parametrize("read", [quality.list_quality_outcomes, quality.get_operator_report])
def test_read_failure_releases_transaction(monkeypatch, read):
    card = make_card(linked=True)

    def fail_read(*args, **kwargs):
        raise RuntimeError("read interrupted")

    monkeypatch.setattr(
        "storage.repositories.runtime.RuntimeRepository.list_quality_outcomes", fail_read
    )
    with SessionLocal() as db:
        with pytest.raises(RuntimeError, match="read interrupted"):
            read(PrintsRepository(db), card["record_id"])
        assert not db.in_transaction()


def test_failed_validation_releases_lock_and_allows_retry():
    card = make_card(linked=True)
    with SessionLocal() as db:
        repo = PrintsRepository(db)
        with pytest.raises(CardError) as error:
            quality.create_quality_outcome(
                repo, card["record_id"], {"result": "accepted"}, actor="inspector"
            )
        assert error.value.code == "invalid_inputs"
        assert not db.in_transaction()
        outcome = quality.create_quality_outcome(repo, card["record_id"], LABEL, actor="inspector")
        assert not db.in_transaction()
        assert len(quality.list_quality_outcomes(repo, card["record_id"])) == 1
        assert outcome["model_retraining_job_id"]


def test_report_builder_uses_detached_inputs_even_on_failure(monkeypatch):
    card = make_card(linked=True)
    with SessionLocal() as db:
        session = db.get(BuildSession, card["session_id"])
        original_group = session.context["runtime_payload"]["group"]
        db.commit()

        def fail_build(**inputs):
            assert not db.in_transaction()
            inputs["group"]["features"]["layers"] = 999
            assert original_group["features"]["layers"] == 10
            raise RuntimeError("report interrupted")

        monkeypatch.setattr("domain.services.operator_report.build_operator_report", fail_build)
        with pytest.raises(RuntimeError, match="report interrupted"):
            quality.get_operator_report(PrintsRepository(db), card["record_id"])
        assert not db.in_transaction()
