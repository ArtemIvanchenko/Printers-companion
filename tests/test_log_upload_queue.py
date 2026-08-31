"""Browser log uploads use the same durable, confirmed import path as watcher files."""

from pathlib import Path

from fastapi.testclient import TestClient

from api.main import app
from core.config.settings import get_settings


def test_upload_creates_confirmable_job_without_overwriting_same_name(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={
        "raw_logs_container_path": str(tmp_path),
        "require_operator_import_confirmation": True,
    })
    monkeypatch.setattr("api.routes.uploads.get_settings", lambda: settings)
    monkeypatch.setattr("api.routes.imports.get_settings", lambda: settings)
    client = TestClient(app)

    first = client.post(
        "/upload/logs",
        files=[("files", ("machine.log", b"first", "text/plain"))],
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["jobs"][0]["status"] == "awaiting_operator_confirmation"
    first_batch = next((tmp_path / "incoming").iterdir())
    assert (first_batch / "machine.log").read_bytes() == b"first"

    second = client.post(
        "/upload/logs",
        files=[("files", ("machine.log", b"second", "text/plain"))],
    )
    assert second.status_code == 200
    second_file = second.json()["saved"][0]
    second_batch = max((tmp_path / "incoming").iterdir(), key=lambda path: path.name)
    assert second_file["stored_name"] == "machine.log"
    assert (first_batch / "machine.log").read_bytes() == b"first"
    assert (second_batch / second_file["stored_name"]).read_bytes() == b"second"
    assert second.json()["jobs"][0]["import_job_id"] != first_body["jobs"][0]["import_job_id"]


def test_uploading_several_logs_creates_one_batch_job(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={
        "raw_logs_container_path": str(tmp_path),
        "require_operator_import_confirmation": True,
    })
    monkeypatch.setattr("api.routes.uploads.get_settings", lambda: settings)
    monkeypatch.setattr("api.routes.imports.get_settings", lambda: settings)
    client = TestClient(app)

    response = client.post(
        "/upload/logs",
        files=[
            ("files", ("01.01.2026.log", b"main", "text/plain")),
            ("files", ("01.01.2026_time.log", b"time", "text/plain")),
        ],
    )

    assert response.status_code == 200
    assert len(response.json()["jobs"]) == 1
    source = response.json()["jobs"][0]["source_path"]
    assert sorted(path.name for path in Path(source).iterdir()) == [
        "01.01.2026.log", "01.01.2026_time.log",
    ]
