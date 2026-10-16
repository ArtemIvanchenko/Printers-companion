"""One scan-parameter precedence for card calculations and STL previews."""

import asyncio
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

import pytest
import trimesh

from analytics.log_insights.geometry import scan_reference
from analytics.prediction.layer_engine import (
    LayerGeometrySeries, physics_scan_seconds_by_layer, resolve_scan_model,
)
from analytics.prediction.plate_estimator import estimate_plate
from analytics.prediction.print_time import PrintTimeEstimate, _resolve_params
from analytics.prediction.scan_scope import scan_scope, scan_scope_key
from analytics.prediction.stl_slicer import SliceResult
from api.routes.uploads import _geometry_prediction
from domain.services.estimation.inputs import resolve_params_with_sources
from storage.repositories.prints_repo import PrintsRepository


@pytest.fixture
def machine():
    return {
        "hatch_speed_mm_s": 1000.0,
        "hatch_speeds_by_mat": {"steel": 2000.0, "aluminum": 950.0},
        "contour_speed_mm_s": 500.0, "hatch_distance_mm": .12,
        "layer_thickness_mm": .1, "laser_count": 1,
        "recoat_time_ms": 1000.0, "contours_enabled": False,
        "jump_speed_mm_s": 1e20, "jump_delay_ms": 0.0,
        "build_origin_z_mm": 0.0, "material_densities": {"steel": 7.9},
    }


@pytest.fixture
def preset():
    return {"preset_id": "fast-preset", "hatch_speed_mm_s": 4000.0}


def _preview_reads(monkeypatch, machine, preset):
    # Detached values only: the preview must not open even the isolated test DB.
    monkeypatch.setattr("storage.db.session.SessionLocal", lambda: nullcontext(None))
    monkeypatch.setattr(PrintsRepository, "get_machine_params", lambda self: machine)
    monkeypatch.setattr(PrintsRepository, "get_active_preset_for_material", lambda *a: preset)
    monkeypatch.setattr(PrintsRepository, "last_powder_cost", lambda self: 45000.0)
    monkeypatch.setattr("analytics.prediction.accuracy.calibration_interval_hours", lambda *a: None)


@pytest.mark.parametrize("preset_value,map_value,expected", [
    (None, {"steel": 2000.0, "aluminum": 950.0}, 2000.0),
    ({"hatch_speed_mm_s": None}, {"steel": 2000.0, "aluminum": 950.0}, 2000.0),
    ({"hatch_distance_mm": .2}, {"steel": 2000.0, "aluminum": 950.0}, 2000.0),
    ({"hatch_speed_mm_s": 4000.0}, {"steel": 2000.0, "aluminum": 950.0}, 4000.0),
    ({"hatch_speed_mm_s": 4000.0}, {}, 4000.0),
    (None, {}, 1000.0),
    (None, {"aluminum": 950.0}, 1000.0),
    ({"hatch_speed_mm_s": 2000.0}, {"steel": 2000.0, "aluminum": 950.0}, 2000.0),
])
def test_material_map_is_pruned_only_for_a_resolved_preset_speed(machine, preset_value, map_value, expected):
    machine["hatch_speeds_by_mat"] = map_value
    original = deepcopy((machine, preset_value))
    params, _ = resolve_params_with_sources({"material": "steel"}, machine, preset_value)
    assert _resolve_params(params, "steel")[0] == expected
    if preset_value and preset_value.get("hatch_speed_mm_s") is not None:
        assert params["hatch_speeds_by_mat"] == {k: v for k, v in map_value.items() if k != "steel"}
    else:
        assert params["hatch_speeds_by_mat"] == map_value
    assert (machine, preset_value) == original


def test_card_speed_still_wins_after_preset_resolution_and_keeps_its_source(machine, preset):
    record = {"material": "steel", "metadata_json": {"scan_strategy": {"hatch_speed_mm_s": 3300.0}}}
    original = deepcopy((record, machine, preset))
    params, sources = resolve_params_with_sources(record, machine, preset)
    assert _resolve_params(params, "steel")[0] == 3300.0
    assert params["hatch_speeds_by_mat"] == {"aluminum": 950.0}
    assert sources["hatch_speed_mm_s"] == {"source": "print_scan_strategy", "value": 3300.0}
    assert (record, machine, preset) == original


def test_hand_computed_physics_and_captured_reference_use_the_same_preset_speed(machine, preset):
    machine.update(contour_speed_mm_s=500.0, support_speed_mm_s=2000.0,
                   jump_speed_mm_s=3000.0, jump_delay_ms=.5, printer_id="physical-test-printer")
    series = LayerGeometrySeries(zs=[0.0, .1], hatch_mm=[1000.0, 1000.0],
        contour_mm=[100.0, 100.0], jump_mm=[30.0, 30.0], n_jumps=[4.0, 4.0],
        open_mm=[50.0, 50.0], z_min=0.0, z_max=.1)
    # Independent arithmetic: hatch + contour + travel + jump delay + support.
    previous = {**machine, "hatch_speed_mm_s": 4000.0}
    before = 1000 / 2000 + 100 / 500 + 30 / 3000 + 4 * .5 / 1000 + 50 / 2000
    after = 1000 / 4000 + 100 / 500 + 30 / 3000 + 4 * .5 / 1000 + 50 / 2000
    assert before == pytest.approx(.737) and after == pytest.approx(.487)
    assert physics_scan_seconds_by_layer(series, .1, previous, "steel", 1) == pytest.approx([before])
    params, _ = resolve_params_with_sources({"material": "steel"}, machine, preset)
    assert physics_scan_seconds_by_layer(series, .1, params, "steel", 1) == pytest.approx([after])
    reference = scan_reference(params, "steel", .1)
    assert reference["beta"] == pytest.approx([1/4000, 1/500, 1/3000, .0005, 1/2000, 0])
    assert scan_scope(params, "steel", .1)["inputs"]["hatch_speed_mm_s"] == 4000.0


def test_existing_scan_scope_rejects_only_the_incompatible_speed_artifact(machine, preset):
    machine["printer_id"] = "physical-test-printer"
    previous = {**machine, "hatch_speed_mm_s": 4000.0, "active_preset_id": "fast-preset"}
    old_scope = scan_scope(previous, "steel", .1)
    model = {"scan_calibration_scope": old_scope, "beta": [.001] * 6}
    machine["scan_model_by_mat"] = {scan_scope_key(old_scope): model}
    previous["scan_model_by_mat"] = machine["scan_model_by_mat"]
    original = deepcopy(machine)
    assert resolve_scan_model(previous, "steel", .1) == model
    params, _ = resolve_params_with_sources({"material": "steel"}, machine, preset)
    assert resolve_scan_model(params, "steel", .1) is None
    new_scope = scan_scope(params, "steel", .1)
    assert old_scope["version"] == new_scope["version"]
    assert old_scope["fingerprint"] != new_scope["fingerprint"]
    assert machine == original
    # An existing compatible artifact remains eligible: no broad invalidation.
    compatible = {"scan_calibration_scope": new_scope, "beta": [.001] * 6}
    params["scan_model_by_mat"] = {scan_scope_key(new_scope): compatible}
    assert resolve_scan_model(params, "steel", .1) == compatible


def test_uncached_native_plate_and_preview_use_the_effective_preset_speed(machine, preset, monkeypatch):
    mesh = trimesh.creation.box(extents=[5.0, 5.0, 1.0])
    mesh.apply_translation([0.0, 0.0, -float(mesh.bounds[0][2])])
    data = mesh.export(file_type="stl")
    original = deepcopy((machine, preset))
    previous = {**machine, "hatch_speed_mm_s": 4000.0}
    params, _ = resolve_params_with_sources({"material": "steel"}, machine, preset)
    # Deliberately no geometry_cache on either call: native sections/paths run.
    before = estimate_plate([("plate.stl", data)], [], previous, "steel")
    after = estimate_plate([("plate.stl", data)], [], params, "steel")
    assert before.scan_source == after.scan_source == "physics"
    assert before.layer_count == after.layer_count == 10
    assert before.raw_scan_hours == pytest.approx(2 * after.raw_scan_hours, rel=1e-12)
    assert after.raw_scan_hours * 3600 == pytest.approx(.4965719749716696, rel=1e-6)
    _preview_reads(monkeypatch, machine, preset)
    preview = asyncio.run(_geometry_prediction(data, "steel"))
    assert preview["available"] is True
    assert preview["time_breakdown"]["hatch_speed"] == 4000.0
    preview_burn = preview["time_breakdown"]["print_hours"] * 3600 - preview["layer_count"]
    assert preview_burn == pytest.approx(after.raw_scan_hours * 3600, rel=1e-12)
    assert (machine, preset) == original


@pytest.mark.parametrize("configuration,preset_value,hatch,powder,speed,resolved_hatch", [
    ("machine", None, None, None, 2000.0, .12),
    ("machine", None, .2, None, 2000.0, .2),
    ("machine", None, None, 55000.0, 2000.0, .12),
    ("machine", {"hatch_speed_mm_s": None}, None, None, 2000.0, .12),
    ("machine", {"hatch_distance_mm": .18}, None, None, 2000.0, .18),
    ("preset_only", None, None, None, 4000.0, .12),
    ("preset_only", None, .2, 55000.0, 4000.0, .2),
])
def test_preview_keeps_ordinary_response_defaults_and_overrides(
    machine, monkeypatch, configuration, preset_value, hatch, powder, speed, resolved_hatch,
):
    if configuration == "preset_only":
        machine = None
        preset_value = {"preset_id": "preset-only", "name": "Полный пресет", "is_default": True,
            "material": "steel", "hatch_speed_mm_s": 4000.0, "contour_speed_mm_s": 1500.0,
            "hatch_distance_mm": .12, "layer_thickness_mm": .1, "laser_power_w": 200.0}
    original = deepcopy((machine, preset_value))
    _preview_reads(monkeypatch, machine, preset_value)
    monkeypatch.setattr("analytics.prediction.stl_slicer.slice_stl", lambda *a:
        SliceResult(volume_mm3=1.0, height_mm=.2, layer_count=2, layer_thickness_mm=.1))
    breakdown = {"hatch_speed": speed, "hatch_distance_mm": resolved_hatch,
                 "laser_count": 1, "layer_count": 2, "prediction_time_scope": "burn_plus_pour"}

    def estimate(slices, params, material, **kwargs):
        assert _resolve_params(params, material)[0] == speed
        assert params["hatch_distance_mm"] == resolved_hatch and params["laser_count"] == 1
        return PrintTimeEstimate(scan_hours=1.0, recoat_hours=2.0, print_hours=3.0,
            raw_scan_hours=.2, raw_recoat_hours=.3, raw_print_hours=.5,
            total_days=None, method="cohatch", breakdown=breakdown, warnings=["time warning"])

    monkeypatch.setattr("analytics.prediction.print_time.estimate_print_time", estimate)
    selected_powder = powder if powder is not None else 45000.0

    def cost(*args, powder_cost_override=None):
        assert powder_cost_override == selected_powder
        return SimpleNamespace(total_rub=10.0, breakdown={"powder_price": selected_powder},
                               powder_kg=.123, prediction=None, warnings=["cost warning"])

    monkeypatch.setattr("analytics.prediction.cost_estimator.estimate_cost", cost)
    actual = asyncio.run(_geometry_prediction(b"unused-by-stub", "steel", hatch, powder))
    assert actual == {
        "available": True, "material": "steel", "method": "cohatch", "layer_count": 2,
        "height_mm": .2, "build_axis": "Z", "scan_hours": 1.0, "recoat_hours": 2.0,
        "print_hours": 3.0, "raw_print_hours": .5, "raw_scan_hours": .2,
        "raw_recoat_hours": .3, "correction_factor": 1.0, "total_days": None,
        "time_breakdown": breakdown, "cost_total_rub": 10.0,
        "cost_breakdown": {"powder_price": selected_powder}, "powder_kg": .123,
        "powder_cost_rub_per_kg": selected_powder, "prediction": None, "cost_prediction": None,
        "warnings": ["time warning", "cost warning"],
    }
    assert (machine, preset_value) == original


@pytest.mark.parametrize("machine,preset,hatch,fields", [
    (None, None, None, "скорость штриховки, скорость контуров, шаг штриховки, толщина слоя"),
    (None, None, .2, "скорость штриховки, скорость контуров, шаг штриховки, толщина слоя"),
    ({}, None, .2, "скорость штриховки, скорость контуров, толщина слоя"),
    (None, {"hatch_speed_mm_s": 4000.0}, None, "скорость контуров, шаг штриховки, толщина слоя"),
])
def test_preview_preserves_missing_configuration_error_parity(machine, preset, hatch, fields, monkeypatch):
    _preview_reads(monkeypatch, machine, preset)
    assert asyncio.run(_geometry_prediction(b"not-read", "steel", hatch)) == {
        "available": False,
        "reason": "Не хватает параметров машины: " + fields
                  + ". Заполните их в Настройки → Параметры машины.",
    }
