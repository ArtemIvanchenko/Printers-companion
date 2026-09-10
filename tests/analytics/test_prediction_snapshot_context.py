from pathlib import Path

from api.routes import prints as prints_module


class _Store:
    def download_file(self, bucket, object_name, destination, *, expected_sha256=None):
        path = Path(destination)
        path.write_bytes(b"stl")
        return path


def test_snapshot_exposes_machine_mode_recoat_geometry_and_cycle(monkeypatch):
    monkeypatch.setattr(prints_module, "ObjectStore", _Store)
    monkeypatch.setattr(prints_module, "_combined_prediction", lambda *args, **kwargs: {
        "available": True,
        "method": "test:cohatch",
        "build_axis": "Z",
        "layer_count": 100,
        "print_hours": 10.0,
        "raw_print_hours": 9.0,
        "raw_scan_hours": 8.0,
        "raw_recoat_hours": 1.0,
        "scan_hours": 9.0,
        "recoat_hours": 1.0,
        "correction_factor": 1.125,
        "scan_source": "fitted",
        "recoat_time_ms": 9250.0,
        "recoat_time_source": "calibrated",
        "machine_cycle_hours": 10.25,
        "layer_overhead_ms": 400.0,
        "layer_overhead_hours": 0.25,
        "minimum_layer_cycle_ms": 20_000.0,
        "minimum_cycle_active_layers": 30,
        "laser_count": 2,
        "cost_total_rub": 100.0,
        "prediction": None,
        "cost_prediction": None,
        "scan_geometry": {"zs": []},
        "geometry_totals": {"hatch_mm": 12345.0},
        "geometry_regions": [{"name": "part.stl", "kind": "part", "z_min_mm": 0, "z_max_mm": 10}],
    })
    prepared = {
        "record": {"record_id": "context", "revision": 2, "metadata_json": {}},
        "platform_files": [{
            "file_name": "part.stl", "file_type": "stl",
            "object_uri": "s3://stls/part", "checksum": "abc",
        }],
        "material": "steel",
        "printer_id": "printer-m350-01",
        "params": {"layer_thickness_mm": 0.06, "hatch_distance_mm": 0.12, "laser_count": 2},
        "powder_cost": None,
    }

    snapshot = prints_module._calculate_prediction_snapshot(prepared)

    assert snapshot["printer_id"] == "printer-m350-01"
    assert snapshot["mode_key"] == "steel@0.060"
    assert snapshot["machine_mode_key"] == "printer-m350-01|steel@0.060|lasers=2"
    assert snapshot["laser_count"] == 2
    assert snapshot["recoat_time_source"] == "calibrated"
    assert snapshot["machine_cycle_hours"] == 10.25
    assert snapshot["time_breakdown"]["machine_hours"] == 10.25
    assert snapshot["time_breakdown"]["minimum_layer_cycle_ms"] == 20_000.0
    assert snapshot["geometry_fingerprint"].startswith("files-sha256:")
    assert "operational_time" not in snapshot
    assert "pause_reserve_hours" not in snapshot
    assert snapshot["calculation_inputs"]["geometry_body_count"] == 1
    assert snapshot["geometry_regions"][0]["name"] == "part.stl"
