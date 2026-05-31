from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from core.config.settings import get_settings


def test_agent_notification_outbox_roundtrip(tmp_path: Path) -> None:
    folder = tmp_path / "incoming" / "telegram_logs"
    folder.mkdir(parents=True)
    (folder / "job.log").write_text("2026-04-28 10:00:00 Start\n", encoding="utf-8")

    client = TestClient(app)
    headers = {"X-API-Token": get_settings().agent_api_token}
    detected = client.post("/agent/import-detected", json={"source_path": str(folder)}, headers=headers)
    assert detected.status_code == 200

    pending = client.get("/agent/notifications/pending", headers=headers)
    assert pending.status_code == 200
    notifications = pending.json()["notifications"]
    assert any("Найдена новая папка логов" in item["text"] for item in notifications)

    notification_id = notifications[0]["notification_id"]
    sent = client.post(f"/agent/notifications/{notification_id}/sent", json={}, headers=headers)
    assert sent.status_code == 200
    assert sent.json()["ok"] is True
