"""Observed open tracks are independent of polygon buffering and ring repair."""

import numpy as np
import networkx as nx
import pytest
import trimesh
from trimesh.path import Path2D
from trimesh.path.entities import Arc, Line

from analytics.prediction import layer_engine
from analytics.prediction.layer_engine import _section_polygons
from analytics.prediction.plate_estimator import estimate_plate
from analytics.prediction.stl_slicer import EstimationError


def _walls(segments):
    vertices, faces = [], []
    for a, b in segments:
        index = len(vertices)
        vertices.extend([(*a, 0), (*b, 0), (*b, 2), (*a, 2)])
        faces.extend([(index, index + 1, index + 2), (index, index + 2, index + 3)])
    return trimesh.Trimesh(vertices=vertices, faces=faces, process=False)


SQUARE = [((0, 0), (10, 0)), ((10, 0), (10, 10)),
          ((10, 10), (0, 10)), ((0, 10), (0, 0))]


@pytest.mark.parametrize("sections", [8, 16, 64])
def test_closed_annulus_has_hole_and_exactly_zero_open_tracks(sections):
    mesh = trimesh.creation.annulus(r_min=3, r_max=6, height=2, sections=sections)
    polygons, open_mm = _section_polygons(mesh, 0)
    assert len(polygons) == 1 and len(polygons[0].interiors) == 1
    assert open_mm == 0


@pytest.mark.parametrize("length", [10.0, .0001])
def test_branch_partly_in_a_closed_line_remains_a_real_open_track(length):
    mesh = _walls([*SQUARE, ((10, 10), (10 + length, 10))])
    path = layer_engine._section_path(mesh, 1)
    # Native entity-level dangling misses the branch: the same entity also
    # contains edges of the square. Edge topology must retain its open portion.
    assert len(path.dangling) == 0
    polygons, open_mm = _section_polygons(mesh, 1)
    plain, _ = _section_polygons(_walls(SQUARE), 1)
    assert open_mm == pytest.approx(length, abs=1e-12)
    assert len(polygons) == 1
    assert polygons[0].area == pytest.approx(plain[0].area, abs=1e-12)
    assert polygons[0].length == pytest.approx(plain[0].length, abs=1e-12)
    assert polygons[0].symmetric_difference(plain[0]).area < 1e-12


def test_mixed_closed_region_and_disconnected_open_walls_keep_both():
    mesh = _walls([*SQUARE, ((20, 0), (23, 0)), ((23, 0), (23, 4))])
    polygons, open_mm = _section_polygons(mesh, 1)
    assert len(polygons) == 1
    assert open_mm == pytest.approx(7)


def test_real_open_sheet_remains_a_single_track_not_a_filled_region():
    mesh = _walls([((0, 0), (30, 0))])
    polygons, open_mm = _section_polygons(mesh, 1)
    assert polygons == [] and open_mm == 30
    params = dict(printer_id="raw-sheet", layer_thickness_mm=.1, hatch_speed_mm_s=1000,
                  contour_speed_mm_s=500, hatch_distance_mm=.1, jump_speed_mm_s=3000,
                  laser_count=2, recoat_time_ms=9000)
    result = estimate_plate([], [("sheet", mesh.export(file_type="stl"))], params, "steel")
    assert result.geometry_totals["hatch_mm"] == 0
    assert result.geometry_totals["contour_mm"] == 0
    assert result.geometry_totals["open_mm"] == pytest.approx(600)
    assert result.scan_hours * 3600 == pytest.approx(.3)


def test_closed_circle_entity_and_open_polyline_keep_native_lengths(monkeypatch):
    vertices = np.array([[5, 0], [0, 5], [-5, 0], [20, 0], [23, 0], [23, 4]], dtype=float)
    path = Path2D(entities=[Arc([0, 1, 2], closed=True), Line([3, 4, 5])],
                  vertices=vertices, process=False)
    monkeypatch.setattr(layer_engine, "_section_path", lambda *args: path)
    polygons, open_mm = _section_polygons(None, 1)
    assert len(polygons) == 1
    assert polygons[0].area == pytest.approx(np.pi * 25, rel=.002)
    assert open_mm == 7


def test_closed_edges_split_over_several_line_entities_are_not_open(monkeypatch):
    path = Path2D(entities=[Line([0, 1]), Line([1, 2]), Line([2, 3]), Line([3, 0])],
                  vertices=[[0, 0], [10, 0], [10, 10], [0, 10]], process=False)
    monkeypatch.setattr(layer_engine, "_section_path", lambda *args: path)
    polygons, open_mm = _section_polygons(None, 1)
    assert len(polygons) == 1 and open_mm == 0


def test_closed_polyline_with_backtracking_branch_counts_each_actual_stroke(monkeypatch):
    points = [0, 1, 2, 4, 2, 3, 0]
    path = Path2D(entities=[Line(points)],
                  vertices=[[0, 0], [10, 0], [10, 10], [0, 10], [20, 10]], process=False)
    monkeypatch.setattr(layer_engine, "_section_path", lambda *args: path)
    polygons, open_mm = _section_polygons(None, 1)
    assert path.entities[0].closed
    assert open_mm == 20  # branch out and back, not coordinate-deduplicated
    assert len(polygons) == 1
    assert polygons[0].area == pytest.approx(100.04, abs=1e-5)


def test_closed_topology_failure_is_not_reclassified_as_open_tracks(monkeypatch):
    path = Path2D(entities=[Line([0, 1, 2, 3, 0])],
                  vertices=[[0, 0], [10, 0], [10, 10], [0, 10]], process=False)
    monkeypatch.setattr(layer_engine, "_section_path", lambda *args: path)

    def broken(self):
        raise ValueError("unrecoverable cycle")

    monkeypatch.setattr(Path2D, "polygons_full", property(broken))
    with pytest.raises(EstimationError, match="замкнутые.*unrecoverable cycle"):
        _section_polygons(None, 1)


@pytest.mark.parametrize("kind", ["multiple_cycles", "nested_holes", "large_annulus"])
def test_proven_cycle_components_keep_exact_polygons_without_bridge_walk(monkeypatch, kind):
    if kind == "multiple_cycles":
        first = trimesh.creation.annulus(r_min=3, r_max=6, height=2, sections=32)
        second = first.copy()
        second.apply_translation([20, 0, 0])
        mesh = trimesh.util.concatenate([first, second])
        z = 0
    elif kind == "large_annulus":
        mesh = trimesh.creation.annulus(r_min=25, r_max=50, height=2, sections=512)
        z = 0
    else:
        segments = []
        for low, high in [(0, 40), (5, 35), (10, 30), (15, 25)]:
            segments.extend([((low, low), (high, low)), ((high, low), (high, high)),
                             ((high, high), (low, high)), ((low, high), (low, low))])
        mesh = _walls(segments)
        z = 1
    native = layer_engine._section_path(mesh, z)
    expected = [layer_engine._to_plate_xy(p, native.metadata["to_3D"])
                .buffer(layer_engine._FIX_EPS).wkb for p in native.polygons_full]

    def forbidden(*args, **kwargs):
        pytest.fail("Degree-two cycle components have no bridges to find")

    monkeypatch.setattr(nx, "bridges", forbidden)
    actual, open_mm = _section_polygons(mesh, z)
    assert open_mm == 0
    assert [p.wkb for p in actual] == expected
    assert sum(len(p.interiors) for p in actual) == (1 if kind == "large_annulus" else 2)


@pytest.mark.parametrize("closed_circle", [False, True])
def test_arc_cycle_reconstruction_keeps_native_polygon_exactly(monkeypatch, closed_circle):
    entities = [Arc([0, 1, 2], closed=closed_circle)]
    if not closed_circle:
        entities.append(Line([2, 0]))
    path = Path2D(entities=entities, vertices=[[5, 0], [0, 5], [-5, 0]], process=False)
    expected = [p.buffer(layer_engine._FIX_EPS).wkb for p in path.polygons_full]
    monkeypatch.setattr(layer_engine, "_section_path", lambda *args: path)

    def forbidden(*args, **kwargs):
        pytest.fail("An arc cycle has no open graph edges")

    monkeypatch.setattr(nx, "bridges", forbidden)
    actual, open_mm = _section_polygons(None, 0)
    assert open_mm == 0
    assert [p.wkb for p in actual] == expected
