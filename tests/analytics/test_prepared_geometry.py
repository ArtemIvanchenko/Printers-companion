"""The single preview consumes its one prepared mesh, not the original defect."""

from dataclasses import replace

import pytest
import trimesh

from analytics.prediction import layer_engine, stl_slicer
from analytics.prediction.plate_estimator import estimate_plate
from analytics.prediction.print_time import estimate_print_time
from analytics.prediction.stl_slicer import EstimationError, SliceResult, slice_stl


PARAMS = dict(
    printer_id="prepared-mesh", layer_thickness_mm=.1, build_origin_z_mm=0,
    hatch_speed_mm_s=1000, contour_speed_mm_s=500, hatch_distance_mm=.1,
    jump_speed_mm_s=3000, laser_count=2, recoat_time_ms=9000,
)


def _box(*, damaged=False):
    mesh = trimesh.creation.box(extents=[10, 10, 2])
    mesh.apply_translation([0, 0, 1])
    if damaged:
        mesh = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces[2:], process=True)
    return mesh.export(file_type="stl")


def test_real_meshfix_repair_is_the_geometry_used_for_time():
    pytest.importorskip("pymeshfix")
    damaged = _box(damaged=True)
    slices = slice_stl(damaged, .1)
    assert slices.was_repaired and slices.is_watertight
    expected = estimate_plate([("whole-box", _box())], [], PARAMS, "steel")
    result = estimate_print_time(slices, PARAMS, "steel", stl_bytes=damaged)
    assert result.scan_hours == pytest.approx(expected.scan_hours, rel=1e-12)
    assert result.scan_hours * 3600 > 13
    assert result.recoat_hours * 3600 == 180
    assert any("починена" in warning for warning in result.warnings)


@pytest.mark.parametrize("damaged", [False, True])
def test_prepared_mesh_is_reused_without_reloading_or_repairing(monkeypatch, damaged):
    if damaged:
        pytest.importorskip("pymeshfix")
    data = _box(damaged=damaged)
    slices = slice_stl(data, .1)
    received = []
    original = layer_engine.compute_layer_series

    def record(meshes, *args, **kwargs):
        received.extend(meshes)
        return original(meshes, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("A prepared consumer must not reload STL or repeat MeshFix")

    monkeypatch.setattr(layer_engine, "compute_layer_series", record)
    monkeypatch.setattr(trimesh, "load", forbidden)
    monkeypatch.setattr(stl_slicer, "_repair_mesh", forbidden)
    first = estimate_print_time(slices, PARAMS, "steel", stl_bytes=data)
    second = estimate_print_time(slices, PARAMS, "steel", stl_bytes=data)
    assert received == [slices.prepared_mesh, slices.prepared_mesh]
    assert first.scan_hours == second.scan_hours


def test_prepared_mesh_is_explicit_and_does_not_change_summary_repr_or_equality():
    slices = slice_stl(_box(), .1)
    legacy = replace(slices, prepared_mesh=None)
    assert slices.prepared_mesh is not None and legacy.prepared_mesh is None
    assert slices == legacy
    assert "prepared_mesh" not in repr(slices)


def test_legacy_slice_summary_still_accepts_unrepaired_stl():
    data = _box()
    prepared = slice_stl(data, .1)
    legacy = replace(prepared, prepared_mesh=None)
    result = estimate_print_time(legacy, PARAMS, "steel", stl_bytes=data)
    expected = estimate_print_time(prepared, PARAMS, "steel", stl_bytes=data)
    assert result.scan_hours == expected.scan_hours


def test_repaired_summary_without_live_mesh_does_not_claim_raw_mesh_is_repaired():
    summary = SliceResult(volume_mm3=200, height_mm=2, layer_count=20,
                          layer_thickness_mm=.1, was_repaired=True)
    with pytest.raises(EstimationError, match="починенн"):
        estimate_print_time(summary, PARAMS, "steel", stl_bytes=_box(damaged=True))
