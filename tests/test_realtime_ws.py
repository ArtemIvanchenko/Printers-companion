"""The realtime telemetry WebSocket must require the service token."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.main import app
from core.config.settings import get_settings


def test_ws_rejects_missing_token() -> None:
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/realtime") as ws:
            ws.receive_json()


def test_ws_rejects_wrong_token() -> None:
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/realtime?token=nope") as ws:
            ws.receive_json()


def test_ws_accepts_valid_token() -> None:
    client = TestClient(app)
    token = get_settings().api_service_token
    with client.websocket_connect(f"/ws/realtime?token={token}") as ws:
        frame = ws.receive_json()
        assert "ts" in frame and "source" in frame
