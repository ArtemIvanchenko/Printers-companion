"""Regression boundaries: the shell is SQL-free; history is compact and lazy."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from api.main import app
from api.routes import dashboard
from domain.models.entities import OperatorEvent, QualityOutcome
from domain.models.sessions import BuildSession
from storage.db.session import engine, session_scope

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def statements():
    queries = []

    def remember(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", remember)
    try:
        yield queries
    finally:
        event.remove(engine, "before_cursor_execute", remember)


def test_landing_shell_does_not_consult_sql(statements):
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert statements == []
    assert "{!" not in response.text
    assert 'id="home-recent-sessions"' in response.text
    assert 'src="http' not in response.text
    assert 'href="https://fonts.' not in response.text


def test_all_shell_scripts_and_styles_are_available_locally():
    client = TestClient(app)
    html = client.get("/").text
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
    assert len(assets) >= 20
    for path in assets:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content, path
    assert not re.search(r"<style>|<script\s*>", html)


@pytest.mark.parametrize("name", ["../VERSION", "%2e%2e%2fVERSION", "vendor/fonts/Figtree-OFL.txt", "missing.js"])
def test_asset_paths_do_not_expose_repository_files(name):
    assert TestClient(app).get("/assets/" + name).status_code == 404


@pytest.mark.parametrize("panel,expected", [("sessions",2),("timeline",2),("quality",4),("consumption",2)])
def test_empty_history_has_constant_bounded_queries(panel, expected, statements):
    response = TestClient(app).get(f"/dashboard/history/{panel}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["items"] == []
    assert payload["total"] == 0
    assert payload["has_more"] is False
    assert len(statements) == expected
    assert any("LIMIT" in query for query in statements)


def test_session_page_projects_fields_without_returning_sensor_payload(statements):
    with session_scope() as db:
        for index in range(12):
            db.add(BuildSession(session_id=f"s{index:02}", classification="REAL_PRINT",
                start_ts=datetime(2026,9,1,tzinfo=timezone.utc), context={"runtime_payload":{"group":{
                    "features":{"duration_min":120, "total_lines":100, "pause_count":0, "burn_events":5},
                    "telemetry":{"secret_large_sensor_array":[1]*5000},
                }}}))
    statements.clear()
    client = TestClient(app)
    first = client.get("/dashboard/history/sessions?limit=10").json()
    second = client.get("/dashboard/history/sessions?limit=10&skip=10").json()
    assert len(first["items"]) == 10 and len(second["items"]) == 2
    assert first["total"] == 12 and first["has_more"] and not second["has_more"]
    assert len({row["id"] for row in first["items"] + second["items"]}) == 12
    assert first["items"][0]["duration_min"] == 120
    assert "secret_large_sensor_array" not in str(first)
    assert len(statements) == 4
    # Selected expressions are JSON projections, never sessions.context itself.
    assert not any(re.search(r"(?:SELECT |, )sessions\.context(?:,|\s+FROM)", sql) for sql in statements)


def test_quality_aggregates_all_records_but_only_loads_one_page():
    now = datetime(2026,9,1,tzinfo=timezone.utc)
    with session_scope() as db:
        for index in range(12):
            db.add(QualityOutcome(outcome_id=f"q{index:02}", timestamp=now,
                inspection_type="visual", result="accepted" if index < 8 else "rejected",
                defect_type="<script>bad</script>" if index >= 8 else None,
                notes="private long note", attachments=[{"not_for_summary":True}]))
    data = TestClient(app).get("/dashboard/history/quality?limit=5").json()
    assert len(data["items"]) == 5 and data["total"] == 12
    assert data["result_counts"] == {"accepted":8,"rejected":4}
    assert "notes" not in data["items"][0] and "attachments" not in data["items"][0]
    data = TestClient(app).get("/dashboard/history/quality?skip=8&limit=5").json()
    assert "<script>" not in data["table_rows"] and "&lt;script&gt;" in data["table_rows"]


def test_consumption_unknown_value_is_not_zero():
    with session_scope() as db:
        for index, value in enumerate([None, "NaN", "0", "3.5"]):
            db.add(OperatorEvent(event_id=f"e{index}", timestamp=datetime(2026,9,1,tzinfo=timezone.utc),
                source_channel="test", event_type="gas_consumption_recorded", value=value))
    data = TestClient(app).get("/dashboard/history/consumption").json()
    assert [item["value"] for item in data["items"]] == [None,None,0,3.5]


@pytest.mark.parametrize("query", ["?limit=101", "?limit=0", "?skip=-1"])
def test_history_page_limits_are_enforced(query):
    assert TestClient(app).get("/dashboard/history/sessions" + query).status_code == 422


def test_history_releases_connection_before_rendering(monkeypatch):
    checked_out = set()
    def checkout(conn, record, proxy):
        checked_out.add(id(conn))
    def checkin(conn, record):
        checked_out.discard(id(conn))
    def render(items):
        assert not checked_out
        return "empty"
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    monkeypatch.setattr(dashboard, "_session_table_rows", render)
    try:
        response = TestClient(app).get("/dashboard/history/sessions")
        assert response.status_code == 200 and response.json()["table_rows"] == "empty"
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_vendor_resources_match_recorded_source_checksums():
    vendor = ROOT / "web_assets/vendor"
    manifest = json.loads((vendor / "manifest.json").read_text())
    for entry in manifest["files"]:
        content = (vendor / entry["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert entry["url"].startswith("https://")


def test_telemetry_list_uses_saved_summary_without_sensor_array(statements):
    with session_scope() as db:
        for index in range(3):
            db.add(BuildSession(session_id=f"telemetry-{index}",
                start_ts=datetime(2026,9,1,tzinfo=timezone.utc), context={"runtime_payload":{"group":{
                    "features":{"duration_min":60},
                    "telemetry":{"time":["10:00"]*5000, "oxygen":{"SO1":[0.1]*5000}},
                    "signal_stats":{"SO1":{"mean":0.1,"group":"oxygen","n":5000}},
                    "health":{"burn_drift":{"mean_sec":15}},
                }}}))
        db.add(BuildSession(session_id="no-telemetry"))
    statements.clear()
    data = TestClient(app).get("/dashboard/telemetry-sessions?limit=2").json()
    assert data["total"] == 3 and len(data["items"]) == 2 and data["has_more"]
    assert data["items"][0]["signal_stats"] == {"SO1":{"mean":0.1,"group":"oxygen"}}
    assert data["items"][0]["mean_burn_seconds"] == 15
    assert len(statements) == 2
    assert "telemetry" not in data["items"][0]
