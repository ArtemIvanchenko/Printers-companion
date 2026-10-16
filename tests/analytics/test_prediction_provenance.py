"""Freeze actual calculator inputs and build identity without changing admission."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
import trimesh

from core.versioning.constants import ANALYSIS_VERSION, APP_VERSION
from core.versioning.provenance import stable_hash
from domain.services.estimation.calculation import calculate_prediction_snapshot, combined_prediction
from domain.services.estimation.inputs import prediction_input_hash


def _box(width, height, x=0.0):
    mesh = trimesh.creation.box(extents=[width, 1.0, height])
    mesh.apply_translation([x, 0.0, -float(mesh.bounds[0][2])])
    return mesh.export(file_type="stl")


@pytest.fixture
def calculator_inputs():
    blobs = {"part.stl": _box(1.0, 1.0002), "support.stl": _box(.4321, .7502, 2.1234)}
    prepared = {
        "record": {"record_id": "provenance", "revision": 3, "metadata_json": {
            "prediction": {"previous": "do-not-copy-previous-forecast"},
            "geometry_quality": {"status": "complete"},
        }},
        "platform_files": [{
            "file_name": name, "file_type": "stl" if name == "part.stl" else "stl_supports",
            "object_uri": "s3://isolated/" + name,
            "checksum": hashlib.sha256(data).hexdigest(),
        } for name, data in blobs.items()],
        "material": "steel", "printer_id": "provenance-printer", "powder_cost": 45000.0,
        "parameter_sources": {"layer_thickness_mm": {"source": "print_record", "value": .1}},
        "params": {
            "printer_id": "provenance-printer", "layer_thickness_mm": .1, "build_origin_z_mm": 0.0,
            "hatch_speed_mm_s": 1000.0, "contour_speed_mm_s": 500.0, "hatch_distance_mm": .12,
            "jump_speed_mm_s": 3000.0, "support_speed_mm_s": 2000.0, "jump_delay_ms": .7654321,
            "contours_enabled": True, "hatch_angle_deg": 67.0,
            "laser_count": 1, "recoat_time_ms": 10000.0, "time_correction_factor": 1.123456789,
            "material_densities": {"steel": 7.9}, "gas_cost_rub_per_atm": 100.0,
            "gas_atm_per_print": 2.0, "platform_cost_rub": 100.0,
            "filter_cost_rub": 5000.0, "filter_lifetime_hours": 100.0,
            "updated_at": "2026-10-13T00:00:00+00:00",
        },
    }

    class Store:
        def download_file(self, bucket, object_name, destination, *, expected_sha256=None):
            data = blobs[object_name]
            assert bucket == "isolated" and hashlib.sha256(data).hexdigest() == expected_sha256
            Path(destination).write_bytes(data)
            return Path(destination)

    return prepared, Store, blobs


def _configuration(prepared):
    from domain.services.estimation.inputs import prediction_configuration
    return prediction_configuration(prepared)


def _scan_model(prepared):
    from analytics.prediction.scan_scope import scan_scope, scan_scope_key
    scope = scan_scope(prepared["params"], prepared["material"], .1)
    model = {"scan_calibration_scope": scope, "beta": [.002, .003, .004, .005, .006, .007],
             "n_layers": 123, "r2": .91, "source_records": ["training-print"],
             "training_session_ids": ["training-session"]}
    prepared["params"]["scan_model_by_mat"] = {scan_scope_key(scope): model}
    return model


def test_configuration_is_detached_and_ignores_only_volatile_machine_timestamp(calculator_inputs):
    prepared, _, _ = calculator_inputs
    original = deepcopy(prepared)
    first = _configuration(prepared)
    assert first["schema_version"] == 1 and first["analysis_version"] == ANALYSIS_VERSION
    assert "updated_at" not in first["params"]
    assert first["material"] == "steel" and first["printer_id"] == "provenance-printer"
    assert first["powder_cost"] == 45000.0
    prepared["params"]["updated_at"] = "2026-10-14T00:00:00+00:00"
    assert stable_hash(_configuration(prepared)) == stable_hash(first)
    first["params"]["material_densities"]["steel"] = 1.0
    assert prepared["params"]["material_densities"] == original["params"]["material_densities"]


@pytest.mark.parametrize("field, value", [
    ("hatch_speed_mm_s", 1200.0), ("recoat_time_ms", 11000.0),
    ("gas_cost_rub_per_atm", 200.0), ("material_densities", {"steel": 8.1}),
    ("scan_model_by_mat", {"mode": {"beta": [1, 2, 3, 4, 5, 6]}}),
    ("layer_cycle_model_by_mode", {"mode": {"minimum_cycle_ms": 13000.0}}),
])
def test_configuration_changes_with_settings_models_and_costs(calculator_inputs, field, value):
    prepared, _, _ = calculator_inputs
    previous = stable_hash(_configuration(prepared))
    prepared["params"][field] = value
    assert stable_hash(_configuration(prepared)) != previous


@pytest.mark.parametrize("field, value", [("material", "titanium"), ("printer_id", "another-printer"),
                                           ("powder_cost", 55000.0)])
def test_configuration_changes_with_effective_record_inputs(calculator_inputs, field, value):
    prepared, _, _ = calculator_inputs
    previous = stable_hash(_configuration(prepared))
    prepared[field] = value
    assert stable_hash(_configuration(prepared)) != previous


def test_configuration_excludes_card_revision_but_publication_fence_still_includes_it(calculator_inputs):
    prepared, _, _ = calculator_inputs
    config = stable_hash(_configuration(prepared))
    publication = prediction_input_hash(prepared)
    prepared["record"]["revision"] += 1
    prepared["record"]["notes"] = "A nonnumeric operator note"
    assert stable_hash(_configuration(prepared)) == config
    assert prediction_input_hash(prepared) != publication


def test_real_calculator_captures_build_config_and_bounded_input_provenance(calculator_inputs, monkeypatch):
    prepared, store, blobs = calculator_inputs
    original, original_blobs = deepcopy(prepared), dict(blobs)
    commit = "a123456789abcdef0123456789abcdef012345678"
    monkeypatch.setenv("GIT_COMMIT", commit)
    monkeypatch.setenv("SOURCE_STATE", "dirty")
    monkeypatch.setenv("BUILD_DATE", "2026-10-13T00:00:00Z")
    snapshot = calculate_prediction_snapshot(prepared, object_store_factory=store, computed_by="isolated-node")
    provenance = snapshot["provenance"]
    assert provenance["component"] == "print-estimate"
    assert provenance["git_sha"] == provenance["build"]["git_sha"] == commit
    assert provenance["build"]["source_state"] == "dirty"
    assert provenance["build"]["built_at"] == "2026-10-13T00:00:00Z"
    assert provenance["build"]["build_id"]
    assert provenance["app_version"] == APP_VERSION and provenance["analysis_version"] == ANALYSIS_VERSION
    assert provenance["generated_by"] == "isolated-node" and provenance["generated_at"]
    assert provenance["config_hash"] == stable_hash(_configuration(prepared))
    assert provenance["input_fingerprint"] == stable_hash({
        "input_hash": snapshot["input_hash"], "geometry_fingerprint": snapshot["geometry_fingerprint"],
    })
    assert snapshot["calculation_inputs"]["configuration_schema_version"] == 1
    assert "do-not-copy-previous-forecast" not in json.dumps(provenance)
    assert prepared == original and blobs == original_blobs


def test_actual_full_correction_factor_reaches_the_captured_physics_beta(calculator_inputs):
    prepared, store, blobs = calculator_inputs
    result = combined_prediction([("part.stl", blobs["part.stl"])],
        [("support.stl", blobs["support.stl"])], "steel", prepared["params"], prepared["powder_cost"])
    factor = prepared["params"]["time_correction_factor"]
    assert result["correction_factor"] == factor
    snapshot = calculate_prediction_snapshot(prepared, object_store_factory=store)
    expected = [factor / 1000.0, factor / 500.0, factor / 3000.0,
                factor * .7654321 / 1000.0, factor / 2000.0, 0.0]
    assert snapshot["correction_factor"] == factor
    assert snapshot["scan_timing_reference"]["beta"] == pytest.approx(expected, rel=1e-15)
    assert snapshot["provenance"]["model_versions"]["scan_reference"] == "preset-path-time-v1"


def test_real_fitted_snapshot_records_exact_applied_scan_identity(calculator_inputs):
    prepared, store, _ = calculator_inputs
    model = _scan_model(prepared)
    snapshot = calculate_prediction_snapshot(prepared, object_store_factory=store)
    assert snapshot["scan_source"] == "fitted"
    reference = snapshot["scan_timing_reference"]
    assert reference["beta"] == model["beta"]
    assert reference["model_version"] == stable_hash(model)
    assert snapshot["provenance"]["model_versions"]["scan_reference"] == stable_hash(model)


@pytest.mark.parametrize("applied", [False, True])
def test_cycle_identity_only_describes_the_actually_applied_contract(calculator_inputs, applied):
    prepared, store, _ = calculator_inputs
    model = {"version": "max_base_floor_v2", "base_overhead_ms": 500.0,
        "base_overhead_status": "identified" if applied else "identified_free_branch",
        "minimum_cycle_status": "identified" if applied else "unidentified",
        "minimum_cycle_ms": 13000.0 if applied else None,
        "minimum_applicable_component_ms": 30000.0}
    prepared["params"]["layer_cycle_model_by_mode"] = {"provenance-printer|steel@0.100|lasers=1": model}
    snapshot = calculate_prediction_snapshot(prepared, object_store_factory=store)
    versions = snapshot["provenance"]["model_versions"]
    if applied:
        assert snapshot["machine_cycle_hours"] is not None
        assert versions["layer_cycle_contract"] == "max_base_floor_v2"
        assert any("артефакта цикла" in note for note in snapshot["provenance"]["limitations"])
    else:
        assert snapshot["machine_cycle_hours"] is None
        assert "layer_cycle_contract" not in versions
        assert not snapshot["provenance"].get("limitations")


def test_pure_parameter_resolver_keeps_precedence_and_input_maps_unchanged():
    from domain.services.estimation.inputs import resolve_params_with_sources
    machine = {"layer_thickness_mm": .03, "hatch_distance_mm": .12, "hatch_speed_mm_s": 1000.0,
               "hatch_speeds_by_mat": {"steel": 1200.0, "aluminum": 950.0}}
    preset = {"preset_id": "preset", "layer_thickness_mm": .05, "hatch_distance_mm": .15}
    record = {"material": "steel", "layer_thickness_mm": .06, "hatch_distance_mm": .2,
              "metadata_json": {"scan_strategy": {"hatch_speed_mm_s": 1330.0,
                  "layer_thickness_mm": .1, "contours_enabled": False, "hatch_angle_deg": 45.0},
                  "build_origin_z_mm": 3.123456789, "process_strategy_id": "strategy-v1"}}
    original = deepcopy((machine, preset, record))
    params, sources = resolve_params_with_sources(record, machine, preset)
    assert params["layer_thickness_mm"] == .1 and params["hatch_distance_mm"] == .2
    assert params["hatch_speed_mm_s"] == 1330.0 and params["hatch_speeds_by_mat"] == {"aluminum": 950.0}
    assert params["active_preset_id"] == "preset" and params["process_strategy_id"] == "strategy-v1"
    assert params["build_origin_z_mm"] == 3.123456789 and params["contours_enabled"] is False
    assert sources["layer_thickness_mm"]["source"] == "print_scan_strategy"
    assert sources["hatch_distance_mm"]["source"] == "print_record"
    assert (machine, preset, record) == original
