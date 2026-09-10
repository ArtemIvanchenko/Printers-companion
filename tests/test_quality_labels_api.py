from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

from api.main import app
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal
from storage.repositories.runtime import RuntimeRepository


client = TestClient(app)


def _card(name: str = "Контрольный образец") -> dict:
    response = client.post("/prints", json={"name": name, "material": "steel"})
    assert response.status_code == 200
    return response.json()


def test_final_quality_label_is_auditable_and_visible_on_card():
    card = _card()
    response = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        headers={"X-Workstation-ID": "operator-pc-03"},
        json={
            "outcome_id": "client_must_not_choose_this",
            "result": "rejected",
            "inspection_type": "ct",
            "inspection_result": "Обнаружены поры 0,4–0,7 мм",
            "defect_type": "porosity",
            "defect_location": "центр детали, верхняя треть",
            "notes": "Повторить контроль после разреза",
        },
    )

    assert response.status_code == 200, response.text
    label = response.json()
    assert label["outcome_id"].startswith("quality_")
    assert label["outcome_id"] != "client_must_not_choose_this"
    assert label["print_record_id"] == card["record_id"]
    assert label["session_id"] is None
    assert label["created_by"] == "operator-pc-03"
    assert label["timestamp"]
    assert label["is_final"] is True

    fetched = client.get(f"/prints/{card['record_id']}").json()
    assert fetched["quality_outcomes"][0]["inspection_result"] == "Обнаружены поры 0,4–0,7 мм"

    report = client.get(f"/prints/{card['record_id']}/operator-report").json()
    assert report["state"]["code"] == "defect_confirmed"
    assert report["quality_outcome"]["defect_location"] == "центр детали, верхняя треть"


def test_rejected_label_requires_type_location_and_inspection_result():
    card = _card()
    endpoint = f"/prints/{card['record_id']}/quality-outcomes"

    assert client.post(endpoint, json={"result": "rejected"}).status_code == 422
    assert client.post(endpoint, json={
        "result": "rejected",
        "inspection_result": "Не соответствует",
        "defect_type": "crack",
    }).status_code == 422
    assert client.post(endpoint, json={
        "result": "warning",
        "inspection_result": "Требуется повторная проверка",
    }).status_code == 422


def test_label_entered_before_logs_follows_card_when_session_is_linked():
    card = _card()
    created = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "dimensional",
            "inspection_result": "Размеры в допуске",
        },
    )
    assert created.status_code == 200, created.text
    card = client.get(f"/prints/{card['record_id']}").json()

    session_id = "session_for_prelabelled_card"
    with SessionLocal() as db:
        db.add(BuildSession(
            session_id=session_id,
            start_ts=datetime(2026, 9, 3, tzinfo=timezone.utc),
            context={"runtime_payload": {"files": [], "group": {
                "classification": "REAL_PRINT",
                "confidence": 0.9,
                "features": {"data_quality_score": 90},
                "data_quality": {"score": 90, "issues": []},
                "health": {"readiness": {"score": 90}, "anomalies": [], "burn_drift": {}},
            }}},
        ))
        db.commit()

    linked = client.patch(
        f"/prints/{card['record_id']}",
        json={"session_id": session_id, "expected_revision": card["revision"]},
    )
    assert linked.status_code == 200, linked.text

    labels = client.get(
        "/quality-outcomes",
        params={"session_id": session_id},
    ).json()
    assert len(labels) == 1
    assert labels[0]["print_record_id"] == card["record_id"]

    report = client.get(f"/sessions/{session_id}/operator-report")
    assert report.status_code == 200, report.text
    assert report.json()["state"]["code"] == "accepted"


def test_label_correction_is_append_only_and_links_to_previous_verdict():
    card = _card("Исправление заключения")
    first = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "visual",
            "inspection_result": "Первичное заключение: годная",
        },
    ).json()
    second_response = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "rejected",
            "inspection_type": "ct",
            "inspection_result": "Повторный КТ выявил пористость",
            "defect_type": "porosity",
            "defect_location": "центр",
            "supersedes_outcome_id": first["outcome_id"],
        },
    )
    assert second_response.status_code == 200, second_response.text
    second = second_response.json()
    assert second["outcome_id"] != first["outcome_id"]
    assert second["supersedes_outcome_id"] == first["outcome_id"]
    history = client.get(f"/prints/{card['record_id']}/quality-outcomes").json()
    assert {row["outcome_id"] for row in history} == {
        first["outcome_id"], second["outcome_id"],
    }


def test_second_final_requires_superseding_the_current_verdict():
    card = _card("Обязательная ссылка на предыдущий итог")
    first = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "visual",
            "inspection_result": "Первичный контроль пройден",
        },
    ).json()

    missing = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "dimensional",
            "inspection_result": "Повторный контроль пройден",
        },
    )

    assert missing.status_code == 409
    assert "обновите карточку" in missing.json()["detail"]
    history = client.get(f"/prints/{card['record_id']}/quality-outcomes").json()
    assert [row["outcome_id"] for row in history] == [first["outcome_id"]]


def test_stale_correction_cannot_branch_the_final_verdict_chain():
    card = _card("Устаревшее исправление")
    first = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "visual",
            "inspection_result": "Первичный итог",
        },
    ).json()
    second = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "rejected",
            "inspection_type": "ct",
            "inspection_result": "КТ выявил пористость",
            "defect_type": "porosity",
            "defect_location": "центр",
            "supersedes_outcome_id": first["outcome_id"],
        },
    ).json()

    stale = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "metallography",
            "inspection_result": "Попытка исправить устаревшую версию",
            "supersedes_outcome_id": first["outcome_id"],
        },
    )

    assert stale.status_code == 409
    history = client.get(f"/prints/{card['record_id']}/quality-outcomes").json()
    assert {row["outcome_id"] for row in history} == {
        first["outcome_id"], second["outcome_id"],
    }


def test_correction_cannot_supersede_a_verdict_from_another_card():
    first_card = _card("Первая карточка")
    other_card = _card("Другая карточка")
    other_final = client.post(
        f"/prints/{other_card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "visual",
            "inspection_result": "Итог другой карточки",
        },
    ).json()

    wrong = client.post(
        f"/prints/{first_card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "visual",
            "inspection_result": "Неверная ссылка на другую карточку",
            "supersedes_outcome_id": other_final["outcome_id"],
        },
    )

    assert wrong.status_code == 409
    assert client.get(f"/prints/{first_card['record_id']}/quality-outcomes").json() == []


def test_generic_quality_endpoint_cannot_spoof_training_grade_label():
    response = client.post(
        "/quality-outcomes",
        json={
            "outcome_id": "client_chosen_quality_id",
            "result": "rejected",
            "is_final": True,
            "inspection_type": "visual",
        },
    )
    assert response.status_code == 200
    assert response.json()["is_final"] is False
    assert response.json()["outcome_id"].startswith("quality_")
    assert response.json()["outcome_id"] != "client_chosen_quality_id"
    assert response.json()["model_retraining_job_id"] is None


def test_generic_create_cannot_overwrite_an_existing_final_label():
    card = _card("Защита итогового заключения")
    final = client.post(
        f"/prints/{card['record_id']}/quality-outcomes",
        json={
            "result": "accepted",
            "inspection_type": "dimensional",
            "inspection_result": "Размеры подтверждены",
        },
    ).json()

    attempted_overwrite = client.post(
        "/quality-outcomes",
        json={
            "outcome_id": final["outcome_id"],
            "result": "unknown",
            "print_record_id": None,
            "notes": "Эта запись не должна заменить итог",
        },
    )
    assert attempted_overwrite.status_code == 200, attempted_overwrite.text
    assert attempted_overwrite.json()["outcome_id"] != final["outcome_id"]

    preserved = client.get(f"/quality-outcomes/{final['outcome_id']}").json()
    assert preserved["is_final"] is True
    assert preserved["result"] == "accepted"
    assert preserved["print_record_id"] == card["record_id"]
    assert preserved["inspection_result"] == "Размеры подтверждены"


def test_repository_rejects_reusing_a_quality_outcome_id():
    outcome_id = "quality_repository_create_only"
    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        repo.create_quality_outcome({
            "outcome_id": outcome_id,
            "result": "accepted",
            "inspection_type": "visual",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        db.commit()

    with SessionLocal() as db:
        repo = RuntimeRepository(db)
        with pytest.raises(ValueError, match="already exists"):
            repo.save_quality_outcome({
                "outcome_id": outcome_id,
                "result": "rejected",
                "inspection_type": "ct",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
