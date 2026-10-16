"""Published computational values retain native precision, not display rounding."""
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import trimesh

from analytics.log_insights.timing import time_accounting
from analytics.prediction.accuracy import prediction_accuracy
from analytics.prediction.calibration_inputs import CalibrationInputs, CalibrationRecord, CalibrationSession
from analytics.prediction.layer_engine import machine_mode_key
from analytics.prediction.plate_estimator import estimate_plate
from analytics.prediction.timing_snapshot import read_timing_publication
from core.versioning.provenance import stable_hash
from domain.services.estimation.calculation import calculate_prediction_snapshot, enrich_prediction_interval
from domain.services.estimation.inputs import geometry_fingerprint, prediction_configuration, prediction_input_hash


def _inputs(origin=0.0):
    blobs = {}
    for name, width, x in (("part.stl", 5.0, 0.0), ("support.stl", .2, 7.0)):
        mesh = trimesh.creation.box(extents=[width, 5.0, 5.0])
        mesh.apply_translation([x, 0.0, origin - float(mesh.bounds[0][2])])
        blobs[name] = mesh.export(file_type="stl")
    prepared = {
        "record": {"record_id": "phase-precision", "revision": 1, "material": "steel",
            "metadata_json": {"geometry_quality": {"status": "complete"},
                "build_origin_z_mm": origin, "session_link_confirmed": True,
                "prediction": {"estimated_at": "2026-09-01", "raw_scan_hours": .006,
                               "raw_recoat_hours": .278, "raw_print_hours": .284}}},
        "platform_files": [{"file_name": name, "file_type": "stl" if name == "part.stl" else "stl_supports",
            "object_uri": "s3://isolated/" + name, "checksum": hashlib.sha256(data).hexdigest()}
            for name, data in blobs.items()],
        "material": "steel", "printer_id": "phase-printer", "powder_cost": 45000., "parameter_sources": {},
        "params": {"printer_id": "phase-printer", "hatch_speed_mm_s": 1550., "contour_speed_mm_s": 500.,
            "support_speed_mm_s": 2000., "hatch_distance_mm": .12, "layer_thickness_mm": .05,
            "laser_count": 1, "recoat_time_ms": 10000., "jump_speed_mm_s": 5000., "jump_delay_ms": 0.,
            "contours_enabled": False, "hatch_angle_deg": 67., "build_origin_z_mm": origin,
            "material_densities": {"steel": 7.9}},
    }

    class Store:
        def download_file(self, bucket, name, destination, *, expected_sha256=None):
            assert bucket == "isolated" and hashlib.sha256(blobs[name]).hexdigest() == expected_sha256
            Path(destination).write_bytes(blobs[name])
            return Path(destination)

    return prepared, Store, blobs


def _estimate(prepared, blobs):
    return estimate_plate([("part.stl", blobs["part.stl"])], [("support.stl", blobs["support.stl"])],
                          prepared["params"], "steel")  # no geometry cache


def _calibration_inputs(snapshots, burn_ms=210.0):
    linked, rows, tags, manifests = [], [], [], {}
    for index, snapshot in enumerate(snapshots):
        session_id = f"phase-timings-{index}"
        when = datetime(2026, 10, index + 1, tzinfo=timezone.utc)
        metadata = {"geometry_quality": {"status": "complete"}, "session_link_confirmed": True,
                    "prediction": snapshot}
        record = CalibrationRecord(f"phase-{index}", session_id, "Synthetic coupon", "steel", .05,
                                   metadata, 2, when)
        session = CalibrationSession(session_id, "phase-printer", when,
                                     when + timedelta(seconds=100 * (burn_ms + 10500) / 1000),
                                     "REAL_PRINT", {})
        linked.append((record, session))
        timed = [{"layer": number, "features": {"burn_ms": burn_ms, "pour_ms": 10000.,
                                                "make_layer_ms": burn_ms + 10500}}
                 for number in range(1, 101)]
        manifest = {"schema": 1, "status": "complete", "row_count": 100, "rows_fingerprint": stable_hash(timed)}
        manifest["publication_id"] = stable_hash(manifest)
        manifests[session_id] = manifest
        tagged = [(row["layer"], row["features"], manifest["publication_id"]) for row in timed]
        assert read_timing_publication(tagged, manifest)[0] == "complete"
        rows.extend((session_id, number, feature) for number, feature, _ in tagged)
        tags.extend(tag for _, _, tag in tagged)
    return CalibrationInputs(linked, rows, None, manifests, tags)


def test_native_phase_precision_reaches_admitted_accuracy_and_preserves_old_capture():
    prepared, store, blobs = _inputs()
    estimate = _estimate(prepared, blobs)
    original = deepcopy((prepared, blobs))
    snapshots = []
    for index in range(3):
        source = deepcopy(prepared)
        source["record"]["record_id"] = f"phase-{index}"
        source["record"]["session_id"] = f"phase-timings-{index}"
        snapshot = calculate_prediction_snapshot(source, object_store_factory=store, computed_by="isolated-node")
        for field in ("raw_scan_hours", "raw_recoat_hours", "raw_print_hours", "scan_hours", "recoat_hours", "print_hours"):
            assert snapshot[field] == pytest.approx(getattr(estimate, field), rel=1e-14)
            assert isinstance(snapshot[field], float)
        assert snapshot["raw_print_hours"] == snapshot["raw_scan_hours"] + snapshot["raw_recoat_hours"]
        assert snapshot["print_hours"] == snapshot["scan_hours"] + snapshot["recoat_hours"]
        assert snapshot["machine_cycle_hours"] is None  # no invented normal cycle
        assert snapshot["input_hash"] == prediction_input_hash(source)
        assert snapshot["geometry_fingerprint"] == geometry_fingerprint(source["platform_files"])
        assert snapshot["provenance"]["config_hash"] == stable_hash(prediction_configuration(source))
        assert snapshot["provenance"]["input_fingerprint"] == stable_hash({
            "input_hash": snapshot["input_hash"], "geometry_fingerprint": snapshot["geometry_fingerprint"]})
        assert snapshot["calculation_inputs"]["configuration_schema_version"] == 1
        snapshots.append(snapshot)

    # Independent integer-ms facts:100*210ms burn and100*10000ms recoat.
    expected_factor = round(21.0 / (estimate.raw_scan_hours * 3600), 3)
    assert 0.5 <= expected_factor <= 2.0
    current = _calibration_inputs(snapshots)
    current_before = deepcopy(current)
    report = prediction_accuracy(inputs=current)
    assert report["n_usable_pairs"] == 3
    assert report["by_mode"]["steel@0.050"]["suggested_factor"] == expected_factor
    assert current == current_before

    # Previously saved compact values remain readable and unchanged, never
    # upgraded by applying today's params or recomputing a historical forecast.
    old = deepcopy(snapshots)
    for snapshot in old:
        for field in ("raw_scan_hours", "raw_recoat_hours", "raw_print_hours", "scan_hours", "recoat_hours", "print_hours"):
            snapshot[field] = round(snapshot[field], 3)
        snapshot["analysis_version"] = "0.5.8"
        snapshot["provenance"]["analysis_version"] = "0.5.8"
        old_configuration = prediction_configuration(prepared)
        old_configuration["analysis_version"] = "0.5.8"
        snapshot["provenance"]["config_hash"] = stable_hash(old_configuration)
    old_before = deepcopy(old)
    old_report = prediction_accuracy(inputs=_calibration_inputs(old))
    assert old_report["n_pairs"] == 3 and old_report["n_usable_pairs"] == 0
    assert old_report["by_mode"]["steel@0.050"]["suggested_factor"] is None
    assert {row["scan_ratio"] for row in old_report["pairs"]} == {.972}
    assert old == old_before and (prepared, blobs) == original


@pytest.mark.parametrize("actual_factor,want_warning", [(1.0, False), (1.5, True)])
def test_native_interval_keeps_equal_point_and_genuinely_outside_warning(actual_factor, want_warning):
    prepared, store, blobs = _inputs()
    estimate = _estimate(prepared, blobs)
    snapshots = []
    for index in range(3):
        source = deepcopy(prepared)
        source["record"]["record_id"] = f"phase-{index}"
        snapshots.append(calculate_prediction_snapshot(source, object_store_factory=store))
    # Synthetic exact native phases test the capture boundary, not held-out
    # physical accuracy or a new coefficient fit. All existing gates still run.
    burn_ms = estimate.raw_scan_hours * 3_600_000 / estimate.layer_count * actual_factor
    history = _calibration_inputs(snapshots, burn_ms=burn_ms)
    assert prediction_accuracy(inputs=history)["n_usable_pairs"] == 3
    target_source = deepcopy(prepared)
    target_source["record"]["record_id"] = "phase-target"
    target = calculate_prediction_snapshot(target_source, object_store_factory=store)
    before = deepcopy(target)
    enrich_prediction_interval(target, inputs=history)
    warned = any("История режима" in warning for warning in target["prediction_warnings"])
    assert warned is want_warning
    expected = actual_factor * target["raw_scan_hours"] + target["raw_recoat_hours"]
    assert target["prediction_interval"] == pytest.approx([expected, expected], rel=1e-14)
    if not want_warning:
        assert target["prediction_interval"] == [target["print_hours"], target["print_hours"]]
    assert target["interval_reference"]["input_fingerprint"] == history.input_fingerprint
    assert target["print_hours"] == before["print_hours"]
    assert target["provenance"] == before["provenance"]


def test_applied_cycle_and_datum_remain_exact_while_display_geometry_stays_compact():
    origin, base, floor = 3.123456789, 333.123456789, 12000.246789
    prepared, store, blobs = _inputs(origin)
    params = prepared["params"]
    params["recoat_time_ms"] = 10000.123456789
    params["time_correction_factor"] = 1.123456789
    key = machine_mode_key("phase-printer", "steel", .05, 1)
    params["layer_cycle_model_by_mode"] = {key: {"version": "max_base_floor_v2",
        "base_overhead_ms": base, "minimum_cycle_ms": floor,
        "base_overhead_status": "identified", "minimum_cycle_status": "identified"}}
    original = deepcopy(prepared)
    estimate = _estimate(prepared, blobs)
    snapshot = calculate_prediction_snapshot(prepared, object_store_factory=store)
    assert snapshot["build_origin_z_mm"] == origin
    assert snapshot["geometry_quality"]["build_origin_z_mm"] == origin
    assert snapshot["layer_overhead_ms"] == snapshot["calculation_inputs"]["layer_overhead_ms"] == base
    assert snapshot["minimum_layer_cycle_ms"] == snapshot["calculation_inputs"]["minimum_layer_cycle_ms"] == floor
    assert snapshot["machine_cycle_hours"] == pytest.approx(estimate.layer_count * floor / 3_600_000, rel=1e-14)
    assert snapshot["machine_cycle_hours"] == pytest.approx(estimate.machine_cycle_hours, rel=1e-14)
    assert snapshot["machine_hours"] == snapshot["time_breakdown"]["machine_hours"] == snapshot["machine_cycle_hours"]
    assert snapshot["scan_hours"] == snapshot["raw_scan_hours"] * 1.123456789
    assert snapshot["recoat_hours"] == snapshot["raw_recoat_hours"]
    # Actual independent saved-model consumer, without changing its formula.
    events = [{"event_type": "layer_timing_summary", "payload": {"layer": number,
        "burn_ms": 210., "pour_ms": params["recoat_time_ms"], "make_layer_ms": floor}} for number in (1, 2)]
    accounting = time_accounting(events, pauses=(), cycle_model=snapshot)
    assert accounting["normal_unique_layer_seconds"] == pytest.approx(2 * floor / 1000, rel=1e-14)
    assert snapshot["scan_geometry"] == {**estimate.geometry_series.to_snapshot(),
        "layer_count": estimate.layer_count, "layer_thickness_mm": .05, "laser_count": 1}
    assert snapshot["scan_geometry"]["z_min"] == round(origin, 3)
    assert snapshot["recoat_time_ms"] == round(estimate.recoat_time_ms, 1)  # display only
    assert snapshot["layer_overhead_hours"] == round(estimate.layer_overhead_hours, 3)  # display only
    assert snapshot["provenance"]["config_hash"] == stable_hash(prediction_configuration(prepared))
    assert prepared == original
