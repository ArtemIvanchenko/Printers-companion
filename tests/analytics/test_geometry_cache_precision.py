"""A geometry cache hit must preserve the prepared computation, not its display rounding."""

import json
from dataclasses import asdict

import numpy as np
import pytest
import trimesh

from analytics.prediction import plate_estimator
from analytics.prediction.layer_engine import GEOMETRY_FEATURES, LayerGeometrySeries
from analytics.prediction.scan_scope import scan_scope, scan_scope_key


class _JsonGeometryCache:
    """Exercise the JSON round trip used by the persistent cache."""

    def __init__(self):
        self.entries = {}
        self.read_keys = []
        self.saved_body_counts = []

    def get_geometry_cache(self, key):
        self.read_keys.append(key)
        value = self.entries.get(key)
        return json.loads(value) if value is not None else None

    def save_geometry_cache(self, key, series_json, body_count):
        self.entries[key] = json.dumps(series_json)
        self.saved_body_counts.append(body_count)


def _precise_series():
    return LayerGeometrySeries(
        zs=[0.050000123, 0.950200033],
        hatch_mm=[1.234567891, 9.876543219],
        contour_mm=[2.345678912, 8.765432198],
        jump_mm=[3.456789123, 7.654321987],
        n_jumps=[4.567891234, 6.543219876],
        open_mm=[5.678912345, 5.432198765],
        z_min=0.000000123,
        z_max=1.000200033,
        body_boundary_mm=[1.234567891, 9.876543219],
        body_active_z_intervals_mm=[[[0.050000123, 0.500200033]], [[0.500200033, 0.950200033]]],
        warnings=["Сохранённое ограничение геометрии"],
    )


def _box_stl(*, width=1.0, height=1.0002, x=0.0):
    mesh = trimesh.creation.box(extents=[width, 1.0, height])
    mesh.apply_translation([x, 0.0, -float(mesh.bounds[0][2])])
    return mesh.export(file_type="stl")


def _params(*, fitted=False, cycle=False):
    params = dict(
        printer_id="cache-precision",
        layer_thickness_mm=0.1,
        build_origin_z_mm=0.0,
        hatch_speed_mm_s=1000.0,
        contour_speed_mm_s=500.0,
        hatch_distance_mm=0.12,
        jump_speed_mm_s=3000.0,
        laser_count=1,
        recoat_time_ms=10000.0,
    )
    if fitted:
        scope = scan_scope(params, "steel", 0.1)
        params["scan_model_by_mat"] = {scan_scope_key(scope): {
            "beta": [0.002, 0.003, 0.004, 0.005, 0.006, 0.007],
            "scan_calibration_scope": scope,
        }}
    if cycle:
        params["layer_cycle_model_by_mode"] = {"cache-precision|steel@0.100|lasers=1": {
            "version": "max_base_floor_v2",
            "base_overhead_ms": 500.0,
            "minimum_cycle_ms": 13000.0,
            "base_overhead_status": "identified",
            "minimum_cycle_status": "identified",
            "n_prints": 4,
            "n_layers": 100,
            "n_geometries": 3,
            "floor_n_layers": 20,
            "floor_n_prints": 2,
        }}
    return params


def test_default_published_snapshot_keeps_existing_compact_values():
    expected = {
        "zs": [0.05, 0.95],
        "hatch_mm": [1.2, 9.9],
        "contour_mm": [2.3, 8.8],
        "jump_mm": [3.5, 7.7],
        "n_jumps": [4.6, 6.5],
        "open_mm": [5.7, 5.4],
        "z_min": 0.0,
        "z_max": 1.0,
        "body_boundary_mm": [1.2, 9.9],
        "body_active_z_intervals_mm": [[[0.05, 0.5]], [[0.5, 0.95]]],
        "warnings": ["Сохранённое ограничение геометрии"],
    }
    assert json.dumps(_precise_series().to_snapshot()) == json.dumps(expected)
    assert _precise_series().to_snapshot(compact=True) == expected


def test_computational_snapshot_preserves_every_float_through_json():
    original = _precise_series()
    data = json.loads(json.dumps(original.to_snapshot(compact=False)))
    restored = LayerGeometrySeries.from_snapshot(data)

    assert data == asdict(original)
    assert restored == original
    assert restored.layer_count(0.1) == original.layer_count(0.1) == 11
    assert restored.totals(0.1) == original.totals(0.1)
    assert restored.body_shares() == original.body_shares()
    assert np.array_equal(restored.at_layers(0.1), original.at_layers(0.1))
    for name in GEOMETRY_FEATURES:
        assert data[name] != original.to_snapshot()[name]


@pytest.mark.parametrize("fitted, cycle", [(False, False), (False, True), (True, True)])
def test_public_boundary_cube_is_identical_on_cold_and_warm_cache(monkeypatch, fitted, cycle):
    stl = _box_stl()
    assert len(stl) == 684
    cache = _JsonGeometryCache()
    calls = []
    original_compute = plate_estimator.compute_layer_series

    def record(*args, **kwargs):
        calls.append(1)
        return original_compute(*args, **kwargs)

    monkeypatch.setattr(plate_estimator, "compute_layer_series", record)
    params = _params(fitted=fitted, cycle=cycle)
    cold = plate_estimator.estimate_plate([("cube", stl)], [], params, "steel", cache)
    warm = plate_estimator.estimate_plate([("cube", stl)], [], params, "steel", cache)

    assert len(calls) == 1 and cache.saved_body_counts == [1]
    assert cold.geometry_series.z_max == 1.0002000331878662
    assert cold.layer_count == warm.layer_count == 11
    assert cold.recoat_hours * 3600 == warm.recoat_hours * 3600 == 110.0
    assert cold.scan_source == ("fitted" if fitted else "physics")
    assert cold.recoat_time_source == "manual"
    if cycle:
        assert cold.minimum_cycle_active_layers == warm.minimum_cycle_active_layers == 11
        assert cold.machine_cycle_hours * 3600 == warm.machine_cycle_hours * 3600 == 143.0
        assert cold.layer_overhead_source == "calibrated_max_base_floor"
    else:
        assert cold.machine_cycle_hours is warm.machine_cycle_hours is None
    assert warm == cold


def test_public_multibody_cache_preserves_shares_intervals_and_totals():
    cache = _JsonGeometryCache()
    parts = [("part", _box_stl())]
    supports = [("support", _box_stl(width=0.4321, height=0.7502, x=2.1234))]
    cold = plate_estimator.estimate_plate(parts, supports, _params(), "steel", cache)
    warm = plate_estimator.estimate_plate(parts, supports, _params(), "steel", cache)

    assert cache.saved_body_counts == [2]
    assert cold.bodies[0].scan_share != cold.bodies[1].scan_share
    assert all(body.active_z_intervals_mm for body in cold.bodies)
    assert warm.geometry_totals == cold.geometry_totals
    assert warm.bodies == cold.bodies
    assert warm == cold


def test_previous_rounded_cache_key_is_not_read_or_rewritten(monkeypatch):
    stl = _box_stl()
    named = [("cube", stl, "part")]
    with monkeypatch.context() as legacy:
        legacy.setattr(plate_estimator, "_GEOMETRY_CACHE_VERSION", 6)
        old_key = plate_estimator._geometry_cache_key(named, 0.12, 0.1, 0.0)
    new_key = plate_estimator._geometry_cache_key(named, 0.12, 0.1, 0.0)
    assert new_key != old_key

    cache = _JsonGeometryCache()
    old_value = json.dumps(_precise_series().to_snapshot())
    cache.entries[old_key] = old_value
    result = plate_estimator.estimate_plate([("cube", stl)], [], _params(), "steel", cache)

    assert result.layer_count == 11
    assert cache.read_keys == [new_key]
    assert cache.entries[old_key] == old_value
    assert cache.saved_body_counts == [1]
    assert json.loads(cache.entries[new_key])["z_max"] == result.geometry_series.z_max
