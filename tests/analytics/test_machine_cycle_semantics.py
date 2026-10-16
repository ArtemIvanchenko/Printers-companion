"""A phase subtotal cannot authorize full-cycle consumers without a cycle model."""
from copy import deepcopy
from pathlib import Path

import pytest

from analytics.prediction import plate_estimator
from analytics.prediction.cost_estimator import estimate_cost
from analytics.prediction.layer_engine import LayerGeometrySeries
from analytics.prediction.print_time import PrintTimeEstimate
from analytics.prediction.stl_slicer import SliceResult
from analytics.prediction.timing_validation import machine_cycle_prediction_hours
from domain.services.estimation.calculation import (
    calculate_prediction_snapshot, combined_prediction,
)
from domain.services.print_cards.comparison import comparison_summary


@pytest.fixture
def plate_inputs(monkeypatch):
    trimesh = pytest.importorskip("trimesh")
    mesh = trimesh.creation.box(extents=[1, 1, .2])
    mesh.apply_translation([0, 0, .1])
    series = LayerGeometrySeries(
        zs=[.05, .15], hatch_mm=[100, 100], contour_mm=[0, 0],
        jump_mm=[0, 0], n_jumps=[0, 0], open_mm=[0, 0],
        z_min=0, z_max=.2, body_boundary_mm=[10],
    )
    monkeypatch.setattr(plate_estimator, "compute_layer_series", lambda *a, **k: series)
    params = {
        "printer_id": "cycle-test", "layer_thickness_mm": .1,
        "build_origin_z_mm": 0, "hatch_speed_mm_s": 100,
        "hatch_distance_mm": .1, "laser_count": 1, "recoat_time_ms": 10_000,
        "filter_cost_rub": 1000, "filter_lifetime_hours": 10,
        "platform_cost_rub": 100, "material_densities": {"steel": 7.9},
        "powder_cost_rub_per_kg": 10,
    }
    return [("part.stl", mesh.export(file_type="stl"))], params


def _model(**overrides):
    return {
        "version": "max_base_floor_v2", "base_overhead_ms": 0,
        "base_overhead_status": "identified", "minimum_cycle_ms": 20_000,
        "minimum_cycle_status": "identified", **overrides,
    }


@pytest.mark.parametrize("model", [
    None,
    _model(version="max_base_floor_v1"),
    _model(base_overhead_ms=None),
    _model(base_overhead_status="unidentified"),
    _model(minimum_cycle_ms=None, minimum_cycle_status="unidentified",
           base_overhead_status="identified_free_branch",
           minimum_applicable_component_ms=22_000),
])
def test_unavailable_controller_model_keeps_phase_sum_only(plate_inputs, model):
    parts, params = plate_inputs
    if model is not None:
        params["layer_cycle_model_by_mode"] = {"cycle-test|steel@0.100|lasers=1": model}
    estimate = plate_estimator.estimate_plate(parts, [], params, "steel")
    assert estimate.print_hours == pytest.approx(22 / 3600)
    assert estimate.machine_cycle_hours is None
    assert estimate.total_days is None
    assert estimate.prediction.value == pytest.approx(estimate.print_hours, abs=.001)


def test_cost_does_not_price_unknown_cycle_as_filter_hours(plate_inputs):
    parts, params = plate_inputs
    estimate = plate_estimator.estimate_plate(parts, [], params, "steel")
    cost = estimate_cost(
        SliceResult(volume_mm3=1, height_mm=.2, layer_count=2, layer_thickness_mm=.1),
        params, "steel", estimate.as_print_time_estimate(),
    )
    assert "фильтр" not in cost.breakdown
    assert cost.breakdown["платформа"] == 100
    assert any("полный цикл" in warning for warning in cost.warnings)


def test_single_part_phase_estimate_cannot_authorize_filter_cycle_cost():
    phases = PrintTimeEstimate(
        scan_hours=9, recoat_hours=1, print_hours=10, total_days=10 / 24, method="fixture",
    )
    cost = estimate_cost(
        SliceResult(volume_mm3=1, height_mm=.2, layer_count=2, layer_thickness_mm=.1),
        {"filter_cost_rub": 1000, "filter_lifetime_hours": 10, "platform_cost_rub": 100},
        "steel", phases,
    )
    assert "фильтр" not in cost.breakdown
    assert cost.total_rub == 100
    assert any("полный цикл" in warning for warning in cost.prediction.warnings)


def test_valid_zero_base_controller_model_retains_cycle_and_filter_cost(plate_inputs):
    parts, params = plate_inputs
    params["layer_cycle_model_by_mode"] = {"cycle-test|steel@0.100|lasers=1": _model()}
    result = combined_prediction(parts, [], "steel", params, None)
    assert result["available"]
    assert result["print_hours"] == pytest.approx(22 / 3600)
    assert result["machine_cycle_hours"] == pytest.approx(40 / 3600)
    assert result["prediction_time_scope"] == "machine_cycle"
    assert result["prediction"]["value"] == .011
    estimate = plate_estimator.estimate_plate(parts, [], params, "steel")
    cost = estimate_cost(
        SliceResult(volume_mm3=1, height_mm=.2, layer_count=2, layer_thickness_mm=.1),
        params, "steel", estimate.as_print_time_estimate(),
    )
    assert cost.breakdown["фильтр"] == 1.11


def test_identified_free_branch_with_zero_base_retains_equal_full_cycle(plate_inputs):
    parts, params = plate_inputs
    params["layer_cycle_model_by_mode"] = {"cycle-test|steel@0.100|lasers=1": _model(
        minimum_cycle_ms=None, minimum_cycle_status="unidentified",
        base_overhead_status="identified_free_branch", minimum_applicable_component_ms=11_000,
    )}
    result = combined_prediction(parts, [], "steel", params, None)
    assert result["machine_cycle_hours"] == result["print_hours"] == pytest.approx(22 / 3600)
    assert machine_cycle_prediction_hours(result) == pytest.approx(22 / 3600)
    assert result["prediction_time_scope"] == "machine_cycle"


def _stored_cycle(**overrides):
    return {
        "machine_cycle_hours": 1.0, "print_hours": .75,
        "layer_overhead_source": "calibrated_max_base_floor",
        "layer_cycle_model_version": "max_base_floor_v2", "layer_overhead_ms": 0.0,
        "minimum_layer_cycle_ms": 20_000.0, "minimum_layer_cycle_status": "identified",
        **overrides,
    }


def test_existing_snapshot_with_controller_evidence_is_still_admitted():
    snapshot = _stored_cycle(input_revision=1)
    before = deepcopy(snapshot)
    assert machine_cycle_prediction_hours(snapshot) == 1.0
    summary = comparison_summary({"revision": 2, "metadata_json": {"prediction": snapshot}})
    assert summary["comparison_scope"] == "machine_cycle"
    assert summary["predicted_hours"] == 1.0
    assert snapshot == before


@pytest.mark.parametrize("changes", [
    {"layer_overhead_source": "outside_calibrated_range"},
    {"layer_cycle_model_version": None},
    {"layer_cycle_model_version": "max_base_floor_v1"},
    {"layer_overhead_ms": None}, {"layer_overhead_ms": True},
    {"minimum_layer_cycle_ms": None}, {"minimum_layer_cycle_ms": float("inf")},
    {"minimum_layer_cycle_status": "unidentified"},
    {"machine_cycle_hours": float("nan")}, {"machine_cycle_hours": True},
    {"machine_cycle_hours": .5}, {"print_hours": float("nan")},
    {"prediction_time_scope": "burn_plus_pour"},
])
def test_unadmitted_stored_cycles_cannot_authorize_full_cycle_consumers(changes):
    assert machine_cycle_prediction_hours(_stored_cycle(**changes)) is None


def test_serializer_retains_nullable_cycle_and_explicit_phase_scope(plate_inputs):
    parts, params = plate_inputs
    result = combined_prediction(parts, [], "steel", params, None)
    assert result["available"]
    assert result["print_hours"] == pytest.approx(22 / 3600)
    assert result["machine_cycle_hours"] is None
    assert result["prediction_time_scope"] == "burn_plus_pour"


class _Store:
    def download_file(self, bucket, name, destination, **kwargs):
        return Path(destination)


@pytest.mark.parametrize("stored_cycle", [None, 4.0])
def test_snapshot_cannot_promote_subtotal_without_controller_evidence(stored_cycle):
    result = {
        "available": True, "print_hours": 4.0, "machine_cycle_hours": stored_cycle,
        "method": "fixture", "layer_count": 2, "cost_total_rub": 100,
        "prediction": {"source": "calculated", "value": 4.0},
    }
    prepared = {
        "record": {"record_id": "cycle", "revision": 1, "metadata_json": {}},
        "platform_files": [{"file_name": "part.stl", "file_type": "stl",
                            "object_uri": "s3://stls/part", "checksum": "test"}],
        "material": "steel", "printer_id": "cycle-test", "powder_cost": None,
        "params": {"layer_thickness_mm": .1},
    }
    snapshot = calculate_prediction_snapshot(
        prepared, object_store_factory=_Store, plate_calculator=lambda *a, **k: result,
    )
    assert snapshot["print_hours"] == 4.0
    assert snapshot["machine_cycle_hours"] is None
    assert snapshot["machine_hours"] is None
    assert snapshot["time_breakdown"]["machine_hours"] is None
    assert snapshot["prediction_time_scope"] == "burn_plus_pour"
    assert snapshot["input_revision"] == 1 and snapshot["input_hash"]


@pytest.mark.parametrize("cycle", [None, 1.0, 9.0])
def test_old_numeric_cycle_is_read_as_phase_scope_without_model_evidence(cycle):
    snapshot = {"print_hours": 1.0, "machine_cycle_hours": cycle, "layer_count": 1,
                "input_revision": 1, "build_origin_source": "explicit", "build_origin_z_mm": 0}
    record = {"revision": 1, "metadata_json": {"prediction": snapshot, "session_link_confirmed": True}}
    before = deepcopy(record)
    summary = comparison_summary(
        record, {"classification": "REAL_PRINT"},
        [(1, {"burn_ms": 2_000_000, "pour_ms": 10_000, "make_layer_ms": 2_050_000}, None)],
    )
    assert summary["comparison_scope"] == "burn_plus_pour"
    assert summary["predicted_hours"] == 1.0
    assert summary["actual_source"] == "subtotal_machine_log"
    assert record == before
