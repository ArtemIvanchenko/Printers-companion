from fastapi.testclient import TestClient

from api.main import app


def test_operator_journal_entry_roundtrip_and_export_payload() -> None:
    client = TestClient(app)
    created = client.post(
        "/operator-journal",
        json={
            "source_channel": "telegram",
            "created_by": "telegram:42",
            "entry_kind": "operator_voice",
            "raw_text": "Поставили новый баллон аргона AG-042",
            "normalized_text": "Поставили новый баллон аргона AG-042",
            "status": "awaiting_operator_confirmation",
        },
    )

    assert created.status_code == 200
    entry = created.json()
    assert entry["journal_entry_id"].startswith("op_journal_")
    assert entry["export_payload"]["schema"] == "operator_journal_entry.v1"
    assert entry["export_payload"]["raw_text"] == "Поставили новый баллон аргона AG-042"

    # operator_event_id is a FK — the referenced row must exist first
    client.post(
        "/operator-events",
        json={"event_id": "op_event_1", "event_type": "note", "raw_text": "test"},
    )

    patched = client.patch(
        f"/operator-journal/{entry['journal_entry_id']}",
        json={"status": "submitted", "operator_event_id": "op_event_1"},
    )

    assert patched.status_code == 200
    assert patched.json()["status"] == "submitted"
    assert patched.json()["export_payload"]["operator_event_id"] == "op_event_1"
