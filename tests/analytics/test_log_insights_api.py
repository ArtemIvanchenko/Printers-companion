from copy import deepcopy

import pytest

from fastapi.testclient import TestClient

from api.main import app
from domain.models.events import LayerSnapshot
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from domain.services.log_insights import context_key
from storage.db.session import SessionLocal

client = TestClient(app)


def snapshot():
    return {"input_revision": 1, "build_origin_source": "explicit", "build_origin_z_mm": 0,
            "geometry_fingerprint": "verified-plate", "printer_id": "M350", "material": "steel",
            "layer_thickness_mm": 1, "hatch_distance_mm": 0.1, "laser_count": 1,
            "process_profile_fingerprint": "preset-1", "layer_overhead_ms": 300,
            "minimum_layer_cycle_ms": 12000,
            "scan_timing_reference": {"beta": [1, 0, 0, 0, 0, 0], "source": "heuristic"},
            "scan_geometry": {"zs": [0, 2], "hatch_mm": [10, 10], "contour_mm": [0, 0],
                              "jump_mm": [0, 0], "n_jumps": [0, 0], "open_mm": [0, 0], "z_min": 0, "z_max": 2}}


def seed(record_id, session_id, burn=10000):
    with SessionLocal() as db:
        db.add(BuildSession(session_id=session_id, classification="REAL_PRINT", context={"runtime_payload": {"group": {
            "log_insights": {"environment": {"metrics": [], "layer_items": []}, "recovery": {"items": []}}
        }}}))
        db.flush()
        db.add(PrintRecord(record_id=record_id, name=record_id, session_id=session_id,
                           metadata_json={"session_link_confirmed": True, "prediction": snapshot()}))
        db.add(LayerSnapshot(session_id=session_id, layer=1, features={"burn_ms": burn, "pour_ms": 1000, "make_layer_ms": burn+1300}))
        db.commit()


def test_all_six_sections_are_available_via_read_only_api():
    seed("r1", "s1")
    seed("r2", "s2", 11000)
    result = client.get("/analysis/prints/r2/log-insights")
    assert result.status_code == 200
    data = result.json()
    assert {"environment", "recovery", "geometry_residuals", "repeatability", "inspection_map", "normal_time_reference"} <= data.keys()
    assert data["geometry_residuals"]["sample_count"] == 1
    assert data["repeatability"]["items"][0]["metrics"]["burn_ms"]["median_change_pct"] == 10.000000000000009
    assert data["provenance"]["input_fingerprint"]


def test_missing_card_and_legacy_card_have_explicit_states():
    assert client.get("/analysis/prints/missing/log-insights").status_code == 404
    with SessionLocal() as db:
        db.add(PrintRecord(record_id="legacy", name="legacy"))
        db.commit()
    result = client.get("/analysis/prints/legacy/log-insights").json()
    assert result["status"] == "needs_reanalysis"
    assert result["geometry_residuals"]["status"] == "unconfirmed_context"


def test_identity_key_does_not_accept_unconfirmed_links_or_recipe_changes():
    record = {"metadata": {"prediction": snapshot(), "session_link_confirmed": True}}
    key = context_key(record)
    assert key
    changed = deepcopy(record)
    changed["metadata"]["prediction"]["hatch_distance_mm"] = 0.12
    assert context_key(changed) != key
    record["metadata"]["session_link_confirmed"] = False
    assert context_key(record) is None


@pytest.mark.parametrize("evidence", [
    {"session_id": "different-session", "method": "operator_selected_session"},
    {"session_id": "this-session", "method": "operator_import_hint"},
])
def test_old_or_date_only_link_evidence_cannot_enable_geometry_decisions(evidence):
    record = {"session_id": "this-session", "metadata": {
        "prediction": snapshot(), "session_link_confirmed": True,
        "session_link_evidence": {"eligible": True, "auto_link_allowed": True, **evidence},
    }}
    assert context_key(record) is None


def test_stale_prediction_does_not_map_observations_to_geometry():
    seed("stale", "stale-session")
    with SessionLocal() as db:
        row = db.get(PrintRecord, "stale")
        row.revision = 20
        db.commit()
    result = client.get("/analysis/prints/stale/log-insights").json()
    assert result["geometry_residuals"]["status"] == "unconfirmed_context"
    assert result["repeatability"]["status"] == "insufficient_identity"


@pytest.mark.parametrize("status", ["incomplete", "lower_bound"])
def test_incomplete_geometry_is_not_used_even_with_confirmed_link_and_origin(status):
    record = {"metadata": {"prediction": snapshot(), "session_link_confirmed": True,
                           "geometry_quality": {"status": status}}}
    assert context_key(record) is None


def test_explicit_link_revocation_overrides_old_auto_link_evidence():
    record = {"metadata": {"prediction": snapshot(), "session_link_confirmed": False,
                           "session_link_evidence": {"eligible": True, "auto_link_allowed": True}}}
    assert context_key(record) is None


def test_ui_script_is_served_and_uses_text_nodes_for_untrusted_values():
    response = client.get("/assets/log-insights.js")
    assert response.status_code == 200
    assert "textContent" in response.text
    assert "innerHTML" not in response.text


def test_each_session_timing_stream_is_prepared_once_after_sql_is_released(monkeypatch):
    from sqlalchemy import event, select
    from analytics.prediction import timing_snapshot
    import domain.services.log_insights as service

    seed("once-r1", "once-s1")
    seed("once-r2", "once-s2", 11000)
    with SessionLocal() as db:
        for sid in ("once-s1", "once-s2"):
            publication = f"publication-{sid}"
            rows = db.scalars(select(LayerSnapshot).where(LayerSnapshot.session_id == sid)
                              .order_by(LayerSnapshot.layer)).all()
            for row in rows:
                row.context = {"publication_id": publication}
            session = db.get(BuildSession, sid)
            session.context = {**session.context, timing_snapshot.MANIFEST_KEY: {
                "schema": 1, "publication_id": publication, "status": "complete",
                "row_count": len(rows), "rows_fingerprint": timing_snapshot.stable_hash([
                    {"layer": row.layer, "features": row.features} for row in rows]),
            }}
        db.commit()
    calls = []
    digests = []
    active = 0
    original = service.calibration_timing_payloads
    original_hash = timing_snapshot.stable_hash

    def checkout(*args):
        nonlocal active
        active += 1

    def checkin(*args):
        nonlocal active
        active -= 1

    def prepare(events):
        assert active == 0
        calls.append([event["payload"]["burn_ms"] for event in events])
        return original(events)

    def fingerprint(rows):
        assert active == 0
        digests.append(rows)
        return original_hash(rows)

    with SessionLocal() as db:
        engine = db.get_bind()
    event.listen(engine, "checkout", checkout)
    event.listen(engine, "checkin", checkin)
    monkeypatch.setattr(service, "calibration_timing_payloads", prepare)
    monkeypatch.setattr(timing_snapshot, "stable_hash", fingerprint)
    try:
        result = service.print_log_insights("once-r2")
        assert result["repeatability"]["sample_count"] == 1
        assert sorted(calls) == [[10000], [11000]]
        assert len(digests) == 2
    finally:
        event.remove(engine, "checkout", checkout)
        event.remove(engine, "checkin", checkin)


def test_unlinked_card_does_not_read_unused_candidates_or_session_tables():
    from sqlalchemy import event
    from domain.services.log_insights import print_log_insights

    with SessionLocal() as db:
        db.add(PrintRecord(record_id="unlinked-read", name="unlinked-read"))
        db.commit()
        engine = db.get_bind()
    reads = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            reads.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        result = print_log_insights("unlinked-read")
        assert result["status"] == "needs_reanalysis"
        assert result["repeatability"]["status"] == "insufficient_identity"
        assert len(reads) == 1
        assert "FROM print_records" in reads[0]
    finally:
        event.remove(engine, "before_cursor_execute", observe)
