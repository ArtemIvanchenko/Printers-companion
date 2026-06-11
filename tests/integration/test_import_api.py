import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from core.config.settings import get_settings


def test_agent_import_callback_uses_buttons_without_terminal(tmp_path: Path) -> None:
    # Use a UUID suffix so the path never collides with a previous run's DB record
    folder = tmp_path / "incoming" / f"button_logs_{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    (folder / "job.log").write_text("2026-04-27 10:00:00 Старт печати\n", encoding="utf-8")
    client = TestClient(app)
    headers = {"X-API-Token": get_settings().agent_api_token}

    detected = client.post("/agent/import-detected", json={"source_path": str(folder)}, headers=headers)
    assert detected.status_code == 200
    job_id = detected.json()["job"]["import_job_id"]
    assert detected.json()["job"]["status"] == "awaiting_operator_confirmation"

    ignored = client.post(
        "/agent/import-callback",
        json={"callback_data": f"import:{job_id}:ignore", "actor": "telegram_user"},
        headers=headers,
    )

    assert ignored.status_code == 200
    assert ignored.json()["job"]["status"] == "ignored"
