"""Published compact geometry retains the estimator's physical layer count."""

import io
import math

import trimesh

from analytics.geometry_context import map_anomalies_to_geometry
from domain.services.estimation.calculation import combined_prediction


def test_compact_snapshot_does_not_drop_the_last_physical_layer():
    mesh = trimesh.creation.box(extents=[1, 1, 1.0002])
    mesh.apply_translation([0, 0, 0.5001])
    source = mesh.export(file_type="stl")
    decoded = trimesh.load_mesh(io.BytesIO(source), file_type="stl")
    expected = math.ceil(float(decoded.bounds[1, 2]) / 0.1)
    result = combined_prediction([("cube", source)], [], "steel", {
        "layer_thickness_mm": 0.1, "hatch_speed_mm_s": 1000.0,
        "hatch_distance_mm": 0.1, "contour_speed_mm_s": 1000.0,
        "jump_speed_mm_s": 1000.0, "laser_count": 2,
        "recoat_time_ms": 10000.0, "build_origin_z_mm": 0.0,
        "time_correction_factor": 1.0,
    }, None)
    assert result["available"]
    assert expected == result["layer_count"] == 11
    geometry = result["scan_geometry"]
    assert geometry["z_max"] == 1.0  # The published display format stays compact.
    assert geometry["layer_count"] == expected
    health = {"burn_drift": {"outlier_layers": [
        {"layer": expected, "duration_sec": 5.0},
        {"layer": expected + 1, "duration_sec": 5.0},
    ]}}
    mapped = map_anomalies_to_geometry(
        health, geometry, geometry_regions=result["geometry_regions"],
        geometry_quality={"build_origin_source": "explicit"},
    )
    assert [item["layer"] for item in mapped["items"]] == [expected]
    assert mapped["items"][0]["mapping_precision"] == "exact_layer"
