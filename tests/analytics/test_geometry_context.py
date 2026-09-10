import pytest

from analytics.geometry_context import map_anomalies_to_geometry
from analytics.prediction.layer_engine import LayerGeometrySeries


def _geometry() -> dict:
    series = LayerGeometrySeries(
        zs=[0.0, 5.0, 10.0],
        hatch_mm=[100.0, 300.0, 100.0],
        contour_mm=[40.0, 50.0, 40.0],
        jump_mm=[10.0, 20.0, 10.0],
        n_jumps=[2.0, 5.0, 2.0],
        open_mm=[0.0, 0.0, 200.0],
        z_min=0.0,
        z_max=10.0,
    )
    return {**series.to_snapshot(), "layer_thickness_mm": 0.1, "layer_count": 100}


def test_maps_exact_layer_and_sensor_progress_to_stl_height():
    health = {
        "anomalies": [{"signal": "SO1", "kind": "spike", "sample_index": 5}],
        "burn_drift": {"outlier_layers": [{"layer": 21, "duration_sec": 80.0}]},
    }
    telemetry = {
        "time": list(range(11)),
        "layer_burn_times": [
            {"layer": layer, "duration_sec": 10.0} for layer in range(1, 101)
        ],
    }
    regions = [
        {
            "name": "detail.stl", "kind": "part", "z_min_mm": 0.0, "z_max_mm": 10.0,
            "xy_bounds_mm": {"x": [-10.0, 10.0], "y": [-10.0, 10.0]},
            "active_z_intervals_mm": [[0.0, 10.0]],
        },
        {
            "name": "support.stl", "kind": "support", "z_min_mm": 0.0, "z_max_mm": 3.0,
            "active_z_intervals_mm": [[0.0, 3.0]],
        },
        {
            # Bounding box crosses the anomaly, but sampled sections prove the
            # disconnected body is absent at this height.
            "name": "arched.stl", "kind": "part", "z_min_mm": 0.0, "z_max_mm": 10.0,
            "active_z_intervals_mm": [[0.0, 1.0], [9.0, 10.0]],
        },
    ]

    result = map_anomalies_to_geometry(
        health, _geometry(), geometry_regions=regions, telemetry=telemetry,
        geometry_quality={"status": "lower_bound"},
    )

    assert result["status"] == "ok"
    assert result["exact_count"] == 1
    assert result["approximate_count"] == 1
    sensor = next(item for item in result["items"] if item["signal"] == "SO1")
    assert sensor["layer"] == 51
    assert sensor["height_mm"] == pytest.approx(5.05, abs=0.01)
    assert sensor["mapping_precision"] == "approximate_progress"
    assert sensor["geometry"]["dominant_operation_ru"] == "штриховка объёма"
    assert [body["name"] for body in sensor["active_bodies"]] == ["detail.stl"]
    assert sensor["active_bodies"][0]["xy_bounds_mm"]["x"] == [-10.0, 10.0]
    assert sensor["active_bodies"][0]["presence_evidence"] == "sampled_section"

    layer = next(item for item in result["items"] if item["anomaly_type"] == "layer_duration")
    assert layer["layer"] == 21
    assert layer["height_mm"] == pytest.approx(2.05, abs=0.01)
    assert layer["mapping_precision"] == "exact_layer"
    assert {body["name"] for body in layer["active_bodies"]} == {"detail.stl", "support.stl"}
    assert result["geometry_confidence"]["level"] == "low"
    assert result["geometry_confidence"]["geometry_quality_status"] == "lower_bound"


def test_missing_geometry_is_an_explicit_normal_outcome():
    result = map_anomalies_to_geometry({"anomalies": [{}]}, None)
    assert result["status"] == "unavailable"
    assert result["items"] == []


def test_timestamp_brackets_define_threshold_layer_range_without_progress_fallback():
    health = {
        "anomalies": [{
            "signal": "SO1",
            "kind": "threshold",
            "sample_ranges": [{
                "start": {
                    "timestamp": "2026-01-01T10:00:00+00:00",
                    "layer_range": [40, 41],
                    "sample_index": 0,
                    "sample_count": 2,
                },
                "end": {
                    "timestamp": "2026-01-01T10:01:00+00:00",
                    "layer_range": [60, 61],
                    "sample_index": 1,
                    "sample_count": 2,
                },
            }],
        }],
    }

    result = map_anomalies_to_geometry(health, _geometry(), telemetry={})

    [item] = result["items"]
    assert item["layer_range"] == [41, 60]
    assert item["mapping_precision"] == "timestamp_range"
