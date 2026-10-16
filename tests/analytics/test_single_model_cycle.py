"""Single-model/plate parity on independent synthetic layer data, without PySLM."""

import pytest
import trimesh

from analytics.prediction import layer_engine, plate_estimator
from analytics.prediction.layer_engine import LayerGeometrySeries
from analytics.prediction.print_time import estimate_print_time
from analytics.prediction.scan_scope import scan_scope, scan_scope_key
from analytics.prediction.stl_slicer import SliceResult
from analytics.prediction.timing_validation import machine_cycle_prediction_hours


@pytest.fixture
def model_inputs(monkeypatch):
    mesh = trimesh.creation.box(extents=[1, 1, .2])
    mesh.apply_translation([0, 0, .1])
    stl = mesh.export(file_type="stl")
    series = LayerGeometrySeries(
        zs=[.05, .15], hatch_mm=[100, 1000], contour_mm=[20, 40],
        jump_mm=[10, 20], n_jumps=[1, 2], open_mm=[60, 120],
        z_min=0, z_max=.2, body_boundary_mm=[10],
    )
    monkeypatch.setattr(layer_engine, "compute_layer_series", lambda *a, **kw: series)
    monkeypatch.setattr(plate_estimator, "compute_layer_series", lambda *a, **kw: series)
    slices = SliceResult(volume_mm3=.2, height_mm=.2, layer_count=2, layer_thickness_mm=.1)
    params = {
        "printer_id": "single-cycle", "layer_thickness_mm": .1, "build_origin_z_mm": 0,
        "hatch_speed_mm_s": 100, "contour_speed_mm_s": 50, "support_speed_mm_s": 30,
        "hatch_distance_mm": .1, "laser_count": 2, "recoat_time_ms": 10_000,
        "jump_speed_mm_s": 1000, "jump_delay_ms": 2,
    }
    return stl, slices, params, series


def _single(inputs):
    stl, slices, params, _ = inputs
    return estimate_print_time(slices, params, "steel", stl_bytes=stl)


def _plate(inputs):
    stl, _, params, _ = inputs
    return plate_estimator.estimate_plate([("model.stl", stl)], [], params, "steel")


def _cycle_model(**overrides):
    return {
        "version": "max_base_floor_v2", "base_overhead_ms": 250,
        "base_overhead_status": "identified", "minimum_cycle_ms": 20_000,
        "minimum_cycle_status": "identified", **overrides,
    }


def _set_cycle(inputs, model, *, key="single-cycle|steel@0.100|lasers=2"):
    inputs[2]["layer_cycle_model_by_mode"] = {key: model}


def test_no_controller_model_keeps_phase_sum_and_unknown_days(model_inputs):
    result = _single(model_inputs)
    assert result.print_hours == pytest.approx(result.scan_hours + result.recoat_hours)
    assert result.total_days is None
    assert result.breakdown["machine_cycle_hours"] is None
    assert result.breakdown["prediction_time_scope"] == "burn_plus_pour"
    assert machine_cycle_prediction_hours(result.breakdown) is None
    assert any("полный цикл" in warning for warning in result.warnings)
    assert "полный нормальный цикл неизвестен" in result.prediction.explanation


def test_single_and_plate_share_support_speed_lasers_and_scan_only_correction(model_inputs):
    model_inputs[2]["time_correction_factor"] = 2
    result, plate = _single(model_inputs), _plate(model_inputs)
    # Per layer: (hatch/100 + contour/50 + open/30 + jump/1000 + n*.002) / 2.
    expected_raw_scan = 1.706 + 7.412
    assert result.raw_scan_hours == pytest.approx(expected_raw_scan / 3600)
    assert result.raw_scan_hours == pytest.approx(plate.raw_scan_hours)
    assert result.scan_hours == pytest.approx(expected_raw_scan * 2 / 3600)
    assert result.recoat_hours == pytest.approx(20 / 3600)
    assert result.print_hours == pytest.approx(plate.print_hours)
    assert result.raw_print_hours == pytest.approx((expected_raw_scan + 20) / 3600)


def _use_nonuniform_sample_geometry(inputs):
    series = inputs[3]
    series.zs = [.05, .09, .15]
    series.hatch_mm = [100, 10000, 100]
    for name in ("contour_mm", "jump_mm", "n_jumps", "open_mm"):
        setattr(series, name, [0, 0, 0])


def test_physics_uses_physical_layer_centres_like_plate(model_inputs):
    _use_nonuniform_sample_geometry(model_inputs)
    result, plate = _single(model_inputs), _plate(model_inputs)
    # Actual layer centres .05/.15 each carry 100 mm; the off-centre peak is not a layer.
    assert result.raw_scan_hours == pytest.approx(1 / 3600)
    assert result.raw_scan_hours == pytest.approx(plate.raw_scan_hours)


def test_existing_build_origin_controls_layers_like_plate(model_inputs, monkeypatch):
    mesh = trimesh.creation.box(extents=[1, 1, .2])
    mesh.apply_translation([0, 0, 1.1])
    stl = mesh.export(file_type="stl")
    series = LayerGeometrySeries(
        zs=[.05, .95, 1.05, 1.15], hatch_mm=[0, 0, 100, 1000],
        contour_mm=[0] * 4, jump_mm=[0] * 4, n_jumps=[0] * 4, open_mm=[0] * 4,
        z_min=0, z_max=1.2, body_boundary_mm=[10],
    )

    def sections(*args, build_origin_z_mm=None, **kwargs):
        assert build_origin_z_mm == 0
        return series

    monkeypatch.setattr(layer_engine, "compute_layer_series", sections)
    monkeypatch.setattr(plate_estimator, "compute_layer_series", sections)
    inputs = stl, model_inputs[1], model_inputs[2], series
    result, plate = _single(inputs), _plate(inputs)
    # Body height is .2 mm, but the supplied build origin requires 12 physical layers.
    assert result.breakdown["layer_count"] == 12
    assert result.recoat_hours == pytest.approx(120 / 3600)
    assert result.raw_scan_hours == pytest.approx(5.5 / 3600)
    assert result.print_hours == pytest.approx(plate.print_hours)


def test_valid_floor_applies_per_layer_after_scan_correction(model_inputs):
    model_inputs[2]["time_correction_factor"] = 2
    _set_cycle(model_inputs, _cycle_model())
    result, plate = _single(model_inputs), _plate(model_inputs)
    # max(1.706*2 + 10 + .25, 20) + max(7.412*2 + 10 + .25, 20).
    expected_cycle = 20 + 25.074
    assert result.breakdown["machine_cycle_hours"] == pytest.approx(expected_cycle / 3600)
    assert result.total_days == pytest.approx(expected_cycle / 86400)
    assert result.breakdown["minimum_cycle_active_layers"] == 1
    assert result.breakdown["prediction_time_scope"] == "machine_cycle"
    assert result.breakdown["layer_overhead_ms"] == 250
    assert machine_cycle_prediction_hours(result.breakdown) == pytest.approx(expected_cycle / 3600)
    assert result.print_hours == pytest.approx(38.236 / 3600)
    assert result.breakdown["machine_cycle_hours"] == pytest.approx(plate.machine_cycle_hours)
    assert result.prediction.value == pytest.approx(round(expected_cycle / 3600, 3))


@pytest.mark.parametrize("model", [
    _cycle_model(version="max_base_floor_v1"),
    _cycle_model(base_overhead_ms=None),
    _cycle_model(base_overhead_status="unidentified"),
    _cycle_model(minimum_cycle_ms=None, minimum_cycle_status="unidentified",
                 base_overhead_status="identified_free_branch", minimum_applicable_component_ms=20_000),
])
def test_unadmitted_controller_models_cannot_create_full_cycle(model_inputs, model):
    _set_cycle(model_inputs, model)
    result, plate = _single(model_inputs), _plate(model_inputs)
    assert result.total_days is None
    assert result.breakdown["machine_cycle_hours"] is None
    assert result.breakdown["layer_overhead_source"] == plate.layer_overhead_source
    assert result.print_hours == pytest.approx(plate.print_hours)


def test_controller_model_for_another_physical_machine_is_not_reused(model_inputs):
    _set_cycle(model_inputs, _cycle_model(), key="other-machine|steel@0.100|lasers=2")
    result = _single(model_inputs)
    assert result.total_days is None
    assert result.breakdown["machine_cycle_hours"] is None


def test_identified_free_branch_uses_only_its_admitted_range(model_inputs):
    _set_cycle(model_inputs, _cycle_model(
        minimum_cycle_ms=None, minimum_cycle_status="unidentified",
        base_overhead_status="identified_free_branch", minimum_applicable_component_ms=10_000,
    ))
    result, plate = _single(model_inputs), _plate(model_inputs)
    assert result.breakdown["machine_cycle_hours"] == pytest.approx(29.618 / 3600)
    assert result.total_days == pytest.approx(plate.total_days)
    assert result.breakdown["minimum_layer_cycle_ms"] is None
    assert result.breakdown["minimum_layer_cycle_status"] == "unidentified"


def test_fitted_scan_remains_absolute_and_shares_embedded_cycle_admission(model_inputs):
    _use_nonuniform_sample_geometry(model_inputs)
    params = model_inputs[2]
    scope = scan_scope(params, "steel", .1)
    params["time_correction_factor"] = 20
    params["scan_model_by_mat"] = {scan_scope_key(scope): {
        "scan_calibration_scope": scope, "beta": [.01, 0, 0, 0, 0, .25],
        "n_layers": 42, "r2": .9, "layer_cycle_model": _cycle_model(),
    }}
    result, plate = _single(model_inputs), _plate(model_inputs)
    # Fitted burn: two layers * (.01*100/2 + .25); no extra factor of 20.
    assert result.raw_scan_hours == pytest.approx(1.5 / 3600)
    assert result.scan_hours == pytest.approx(result.raw_scan_hours)
    assert result.correction_factor == 1
    assert result.prediction.source.value == "model"
    assert result.prediction.sample_size == 42
    assert result.breakdown["machine_cycle_hours"] == pytest.approx(40 / 3600)
    assert result.total_days == pytest.approx(plate.total_days)
