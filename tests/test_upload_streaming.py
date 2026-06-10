"""Upload endpoint streams to disk and enforces the size limit mid-stream.

Guards the fix that replaced `await f.read()` (whole file into RAM) with chunked
streaming, so a large upload can't OOM the memory-capped api container.
"""
import io

from fastapi.testclient import TestClient

import api.routes.uploads as uploads
from api.main import app
from core.config.settings import get_settings


def test_oversized_upload_is_rejected_and_not_left_on_disk(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(tmp_path))
    # Shrink the limit so a tiny payload trips it without allocating anything big.
    monkeypatch.setattr(uploads, "_MAX_FILE_MB", 0.0001)  # ~104 bytes

    client = TestClient(app)
    payload = b"x" * 5000  # comfortably over ~104 bytes
    resp = client.post(
        "/upload/logs",
        files={"files": ("big.log", io.BytesIO(payload), "text/plain")},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] == []
    assert body["skipped"] and body["skipped"][0]["name"] == "big.log"
    # The partial/oversized file must be cleaned up, not left behind.
    assert not (tmp_path / "big.log").exists()


def test_unsupported_suffix_is_skipped(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "raw_logs_container_path", str(tmp_path))

    client = TestClient(app)
    resp = client.post(
        "/upload/logs",
        files={"files": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["saved"] == []
    assert body["skipped"][0]["reason"] == "неподдерживаемый тип файла"
    assert not (tmp_path / "notes.txt").exists()
