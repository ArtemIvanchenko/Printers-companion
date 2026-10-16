import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from api.main import app
from api.routes.uploads import _historical_rate
from domain.enums.common import SessionClassification
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal, engine


def seed_duration(session_id, duration, classification=SessionClassification.real_print.value, *, giant=False):
    group = {"classification": classification, "features": {"duration_min": duration}}
    if giant:
        group["telemetry"] = {"raw_legacy_samples": list(range(100000))}
    with SessionLocal() as db:
        db.add(BuildSession(session_id=session_id, classification=classification,
                            context={"runtime_payload": {"group": group}}, status="runtime_payload"))
        db.commit()


def test_historical_reference_excludes_service_idle_and_unknown_sessions():
    seed_duration("print", 60)
    seed_duration("resumed", 120, SessionClassification.real_print_with_resume.value)
    seed_duration("service", 1000, SessionClassification.service_session.value)
    seed_duration("idle", 2000, SessionClassification.idle_diagnostic.value)
    seed_duration("unknown", 3000, SessionClassification.incomplete_or_unknown.value)
    assert _historical_rate() == {"sessions_used": 2, "avg_duration_min": 90}


def test_reference_uses_shared_payload_first_classification_contract():
    seed_duration("legacy-real", 60)
    seed_duration("contradictory-service", 200)
    seed_duration("scalar-real", 120)
    with SessionLocal() as db:
        legacy = db.get(BuildSession, "legacy-real")
        legacy.classification = SessionClassification.incomplete_or_unknown.value
        service = db.get(BuildSession, "contradictory-service")
        service.context = {"runtime_payload": {"group": {
            "classification": SessionClassification.service_session.value,
            "features": {"duration_min": 200}}}}
        scalar = db.get(BuildSession, "scalar-real")
        scalar.context = {"runtime_payload": {"group": {"features": {"duration_min": 120}}}}
        db.commit()
    assert _historical_rate() == {"sessions_used": 2, "avg_duration_min": 90}


@pytest.mark.parametrize("bad", [None, "bad", "NaN", "Infinity", True, False])
def test_invalid_duration_does_not_erase_other_known_history(bad):
    seed_duration("valid", 60)
    seed_duration("invalid", bad)
    assert _historical_rate() == {"sessions_used": 1, "avg_duration_min": 60}


def test_numeric_legacy_string_retains_existing_reference_capability():
    seed_duration("numeric-string", "120.0")
    assert _historical_rate() == {"sessions_used": 1, "avg_duration_min": 120}


def test_reference_preserves_existing_minimum_elapsed_window_filter():
    seed_duration("short", 10)
    seed_duration("zero", 0)
    seed_duration("negative", -20)
    assert _historical_rate() == {"sessions_used": 0, "avg_duration_min": None}


def test_scalar_projection_does_not_fetch_legacy_large_context():
    seed_duration("large-a", 60, giant=True)
    seed_duration("large-b", 120, giant=True)
    columns = []
    fetch_batches = []
    def after_cursor_execute(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            columns.append([description[0] for description in cursor.description])
            fetch_batches.append(context.execution_options.get("yield_per"))
    event.listen(engine, "after_cursor_execute", after_cursor_execute)
    try:
        assert _historical_rate() == {"sessions_used": 2, "avg_duration_min": 90}
    finally:
        event.remove(engine, "after_cursor_execute", after_cursor_execute)
    assert columns == [["duration_min"]]
    assert fetch_batches == [500]


def test_average_of_large_finite_values_stays_finite_and_json_safe():
    seed_duration("large-a", 1e308)
    seed_duration("large-b", 1e308)
    result = _historical_rate()
    assert result == {"sessions_used": 2, "avg_duration_min": 1e308}
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_invalid_json_numeric_literals_cannot_poison_valid_sqlite_history(bad):
    seed_duration("valid", 60)
    seed_duration("invalid", bad)
    assert _historical_rate() == {"sessions_used": 1, "avg_duration_min": 60}


def test_stl_endpoint_displays_elapsed_reference_without_substituting_geometry_prediction(monkeypatch):
    seed_duration("valid", 90)
    async def unavailable(*args, **kwargs):
        return {"available": False, "reason": "fixture geometry unavailable"}
    monkeypatch.setattr("api.routes.uploads._geometry_prediction", unavailable)
    # A valid empty binary STL is sufficient: this test exercises historical
    # reference display, not the separately tested geometry estimator.
    response = TestClient(app).post("/upload/stl-estimate",
                                   files={"file": ("empty.stl", b"\x00" * 84, "model/stl")})
    assert response.status_code == 200
    body = response.json()
    assert body["historical"] == {"sessions_used": 1, "avg_duration_min": 90}
    assert body["estimate"]["avg_session_hours"] == 1.5
    assert "печат" in body["estimate"]["note"]
    assert "пауз" in body["estimate"]["note"]
    assert body["prediction"] == {"available": False, "reason": "fixture geometry unavailable"}
