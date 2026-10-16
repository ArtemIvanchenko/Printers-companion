"""Supported QC coordinates are physical 1-based ranges, not arbitrary notes."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import select

from api.main import app
from domain.models.jobs import BackgroundJob
from domain.models.quality import QualityOutcome
from domain.services.quality import FinalPrintOutcomeDraft, QualityOutcomeDraft
from storage.db.session import SessionLocal


def rejected(**values):
    return {"result": "rejected", "inspection_result": "КТ выявил поры",
            "defect_type": "porosity", **values}


@pytest.mark.parametrize("raw,expected", [
    ({"start_layer": 1, "end_layer": 1}, {"start_layer": 1, "end_layer": 1}),
    ({"start_layer": 10, "end_layer": 50}, {"start_layer": 10, "end_layer": 50}),
    ({"start_layer": "10", "end_layer": "50"}, {"start_layer": 10, "end_layer": 50}),
    ({"start_layer": 10.0, "end_layer": 50.0}, {"start_layer": 10, "end_layer": 50}),
    ({"start_layer": 1, "end_layer": 10_000_000}, {"start_layer": 1, "end_layer": 10_000_000}),
    ({"start_layer": 1, "end_layer": 50, "legacy_note": 7},
     {"start_layer": 1, "end_layer": 50, "legacy_note": 7}),
])
def test_supported_range_retains_existing_integer_coercion_and_unknown_annotations(raw, expected):
    assert FinalPrintOutcomeDraft(**rejected(layer_range=raw)).layer_range == expected


@pytest.mark.parametrize("raw", [
    {"start_layer": 100, "end_layer": 50},
    {"start_layer": 0, "end_layer": 50},
    {"start_layer": -1, "end_layer": 50},
    {"start_layer": 1, "end_layer": 0},
    {"start_layer": 1},
    {"end_layer": 10},
    {"start_layer": None, "end_layer": 10},
    {"start_layer": 1, "end_layer": None},
    {"start_layer": True, "end_layer": 10},
    {"start_layer": 1, "end_layer": False},
    {"start_layer": 1.5, "end_layer": 10},
    {"start_layer": 1, "end_layer": 10.5},
    {"start_layer": float("nan"), "end_layer": 10},
    {"start_layer": 1, "end_layer": float("inf")},
])
@pytest.mark.parametrize("location", [None, "Верхний край детали"])
def test_invalid_supported_coordinates_are_rejected_even_with_a_text_location(raw, location):
    with pytest.raises(ValidationError):
        FinalPrintOutcomeDraft(**rejected(layer_range=raw, defect_location=location))


@pytest.mark.parametrize("annotation", [
    {"operator_code": 17}, {"start": 0, "end": 100}, {"legacy_index": -3},
])
def test_unknown_annotation_is_preserved_but_not_guessed_as_a_supported_location(annotation):
    generic = QualityOutcomeDraft(result="unknown", layer_range=annotation)
    assert generic.layer_range == annotation
    final = FinalPrintOutcomeDraft(**rejected(defect_location="Край детали", layer_range=annotation))
    assert final.layer_range == annotation
    with pytest.raises(ValidationError):
        FinalPrintOutcomeDraft(**rejected(layer_range=annotation))


def test_interim_partial_notes_remain_interim_and_are_not_promoted_to_ranges():
    result = QualityOutcomeDraft(result="unknown", layer_range={"start_layer": 10})
    assert result.layer_range == {"start_layer": 10}
    assert result.is_final is False


client = TestClient(app)


def card():
    response = client.post("/prints", json={"name": "Изолированная проверка QC диапазона"})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("raw", [
    {"start_layer": 100, "end_layer": 50}, {"start_layer": 0, "end_layer": 50},
    {"start_layer": 1.5, "end_layer": 50}, {"start_layer": 1},
    {"operator_code": 7},
])
def test_invalid_final_range_returns_422_without_publishing_label_revision_or_job(raw):
    record = card()
    response = client.post(f"/prints/{record['record_id']}/quality-outcomes",
                           json=rejected(layer_range=raw))
    assert response.status_code == 422, response.text
    current = client.get(f"/prints/{record['record_id']}").json()
    assert current["revision"] == record["revision"]
    assert current["quality_outcomes"] == []
    with SessionLocal() as db:
        assert db.scalar(select(QualityOutcome)) is None
        assert db.scalar(select(BackgroundJob)) is None


def test_valid_range_roundtrip_and_operator_projection_preserve_exact_coordinates():
    record = card()
    expected = {"start_layer": 1, "end_layer": 100, "legacy_annotation": 7}
    response = client.post(f"/prints/{record['record_id']}/quality-outcomes",
                           json=rejected(layer_range=expected))
    assert response.status_code == 200, response.text
    with SessionLocal() as db:
        stored = db.get(QualityOutcome, response.json()["outcome_id"])
        assert stored.layer_range == expected and stored.is_final is True
    report = client.get(f"/prints/{record['record_id']}/operator-report").json()
    assert report["state"]["code"] == "defect_confirmed"
    assert report["quality_outcome"]["layer_range"] == expected


def test_unknown_annotation_with_text_location_and_generic_partial_note_survive_reload():
    record = card()
    endpoint = f"/prints/{record['record_id']}/quality-outcomes"
    annotation = {"legacy_index": -3}
    response = client.post(endpoint, json=rejected(
        layer_range=annotation, defect_location="Верхний край детали",
    ))
    assert response.status_code == 200, response.text
    observation = client.post("/quality-outcomes", json={
        "print_record_id": record["record_id"], "result": "unknown",
        "layer_range": {"start_layer": 10}, "inspection_result": "Промежуточная заметка",
    })
    assert observation.status_code == 200, observation.text
    with SessionLocal() as db:
        assert db.get(QualityOutcome, response.json()["outcome_id"]).layer_range == annotation
        note = db.get(QualityOutcome, observation.json()["outcome_id"])
        assert note.layer_range == {"start_layer": 10} and note.is_final is False
    report = client.get(f"/prints/{record['record_id']}/operator-report").json()
    assert report["quality_outcome"]["layer_range"] == annotation
    assert report["quality_outcome"]["defect_location"] == "Верхний край детали"


def test_bad_range_correction_keeps_the_current_verdict_and_revision():
    record = card()
    endpoint = f"/prints/{record['record_id']}/quality-outcomes"
    first = client.post(endpoint, json={"result": "accepted", "inspection_result": "Размеры в допуске"})
    assert first.status_code == 200, first.text
    before = client.get(f"/prints/{record['record_id']}").json()
    response = client.post(endpoint, json=rejected(
        layer_range={"start_layer": 100, "end_layer": 50},
        supersedes_outcome_id=first.json()["outcome_id"],
    ))
    assert response.status_code == 422, response.text
    after = client.get(f"/prints/{record['record_id']}").json()
    assert after["revision"] == before["revision"]
    assert after["quality_outcomes"] == before["quality_outcomes"]
    assert client.get(f"/prints/{record['record_id']}/operator-report").json()["state"]["code"] == "accepted"


def test_existing_legacy_annotation_is_read_without_validation_or_rewriting():
    record = card()
    old_range = {"start_layer": 100, "end_layer": 50}
    with SessionLocal() as db:
        db.add(QualityOutcome(
            outcome_id="legacy-reversed-range", print_record_id=record["record_id"],
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc), inspection_type="ct",
            result="rejected", is_final=True, inspection_result="Историческое заключение",
            defect_type="porosity", layer_range=old_range,
        ))
        db.commit()
    history = client.get(f"/prints/{record['record_id']}/quality-outcomes").json()
    assert history[0]["layer_range"] == old_range
    assert client.get(f"/prints/{record['record_id']}/operator-report").json()["quality_outcome"]["layer_range"] == old_range
    with SessionLocal() as db:
        assert db.get(QualityOutcome, "legacy-reversed-range").layer_range == old_range
