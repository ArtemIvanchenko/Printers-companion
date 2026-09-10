from copy import deepcopy

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


def test_stale_prediction_does_not_map_observations_to_geometry():
    seed("stale", "stale-session")
    with SessionLocal() as db:
        row = db.get(PrintRecord, "stale")
        row.revision = 20
        db.commit()
    result = client.get("/analysis/prints/stale/log-insights").json()
    assert result["geometry_residuals"]["status"] == "unconfirmed_context"
    assert result["repeatability"]["status"] == "insufficient_identity"


def test_ui_script_is_served_and_uses_text_nodes_for_untrusted_values():
    response = client.get("/assets/log-insights.js")
    assert response.status_code == 200
    assert "textContent" in response.text
    assert "innerHTML" not in response.text
