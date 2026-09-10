import hashlib

from fastapi.testclient import TestClient

from api.main import app
from core.config.settings import get_settings
from storage.sync.local_outbox import LocalNasOutbox


client = TestClient(app)


def test_sync_status_is_local_and_reports_waiting_bytes(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={
        "app_env": "test",
        "compute_node_id": "operator-api",
        "nas_outbox_path": str(tmp_path / "outbox"),
        "nas_outbox_max_bytes": 1000,
    })
    monkeypatch.setattr("api.routes.sync.get_settings", lambda: settings)
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"waiting")
    LocalNasOutbox.from_settings(settings).enqueue_attachment(
        payload,
        owner_node_id="operator-api",
        record_id="pr_waiting",
        file_name="payload.bin",
        file_type="doc",
        bucket="docs",
        checksum=hashlib.sha256(b"waiting").hexdigest(),
        size_bytes=7,
    )

    response = client.get("/sync/status")

    assert response.status_code == 200
    body = response.json()
    assert body["compute_node_id"] == "operator-api"
    assert body["nas_does_compute"] is False
    assert body["outbox"]["counts"]["pending"] == 1
    assert body["outbox"]["bytes_waiting"] == 7
