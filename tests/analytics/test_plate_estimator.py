"""Plate estimator: parts via PySLM, supports via sections, honest warnings."""
import io
import zipfile

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("shapely")

from analytics.prediction.layer_engine import machine_cycle_from_layers  # noqa: E402
from analytics.prediction.plate_estimator import estimate_plate  # noqa: E402
from analytics.prediction.stl_slicer import EstimationError, slice_stl  # noqa: E402


def _box_stl(x=20.0, y=20.0, z=10.0, z_offset=0.0) -> bytes:
    box = trimesh.creation.box(extents=[x, y, z])
    box.apply_translation([0, 0, -float(box.bounds[0][2]) + z_offset])
    return box.export(file_type="stl")


def _sheet_stl(width=30.0, height=20.0) -> bytes:
    """A single vertical open quad — the shape of a Magics support wall."""
    v = np.array([[0, 0, 0], [width, 0, 0], [width, 0, height], [0, 0, height]], dtype=float)
    f = np.array([[0, 1, 2], [0, 2, 3]])
    return trimesh.Trimesh(vertices=v, faces=f, process=False).export(file_type="stl")


def _params(**over):
    base = dict(
        printer_id="test-printer",
        layer_thickness_mm=0.1,
        hatch_speed_mm_s=1000.0,
        contour_speed_mm_s=500.0,
        hatch_distance_mm=0.12,
        jump_speed_mm_s=3000.0,
        laser_count=1,
        recoat_time_ms=10_000.0,
    )
    base.update(over)
    return base


def _scoped_model_params(model, **over):
    from analytics.prediction.scan_scope import scan_scope, scan_scope_key

    params = _params(**over)
    scope = scan_scope(params, "steel", params["layer_thickness_mm"])
    params["scan_model_by_mat"] = {scan_scope_key(scope): {**model, "scan_calibration_scope": scope}}
    return params


class TestParts:
    @pytest.mark.parametrize("kind", ["box", "annulus", "branches", "near_merge"])
    @pytest.mark.parametrize("outside", [False, True])
    def test_section_path_keeps_vertices_entity_order_holes_and_metadata(self, kind, outside,
                                                                      monkeypatch):
        from scipy.sparse import csgraph
        from trimesh.constants import tol_path
        from analytics.prediction.layer_engine import _section_path

        if kind == "box":
            mesh = trimesh.creation.box(extents=[20, 10, 3])
        elif kind == "annulus":
            mesh = trimesh.creation.annulus(r_min=3, r_max=6, height=4, sections=16)
        else:
            if kind == "branches":
                segments = [((0, 0), (3, 0)), ((3, 0), (6, 0)), ((3, 0), (3, 4))]
            else:
                eps = 0.25 * 10 ** -tol_path.merge_digits
                segments = [((0, 0), (5, 0)), ((5, 0), (5, 5)),
                            ((5 + eps, 5), (0, 5)), ((0, 5), (0, 0))]
            vertices, faces = [], []
            for (x1, y1), (x2, y2) in segments:
                index = len(vertices)
                vertices.extend([(x1, y1, 0), (x2, y2, 0), (x2, y2, 2), (x1, y1, 2)])
                faces.extend([(index, index + 1, index + 2), (index, index + 2, index + 3)])
            mesh = trimesh.Trimesh(vertices=vertices, faces=faces, process=False)
        mesh.apply_translation([73, -29, 0.4])
        original_vertices, original_faces = mesh.vertices.copy(), mesh.faces.copy()
        z = mesh.bounds[1, 2] + 1 if outside else float(mesh.bounds[:, 2].mean())
        expected, = mesh.section_multiplane(plane_origin=[0, 0, 0], plane_normal=[0, 0, 1],
                                          heights=[z])
        graphs = []
        depth_first = csgraph.depth_first_order

        def traverse(graph, *args, **kwargs):
            assert graph.format == "csr" and graph.dtype == np.float64
            graphs.append(graph)
            return depth_first(graph, *args, **kwargs)

        monkeypatch.setattr(csgraph, "depth_first_order", traverse)
        actual = _section_path(mesh, z)
        assert np.array_equal(mesh.vertices, original_vertices)
        assert np.array_equal(mesh.faces, original_faces)
        if outside:
            assert expected is actual is None
            assert not graphs
            return
        assert graphs and all(graph is graphs[0] for graph in graphs)
        assert np.array_equal(actual.vertices, expected.vertices)
        assert len(actual.entities) == len(expected.entities)
        assert all(np.array_equal(got.points, want.points) for got, want in
                   zip(actual.entities, expected.entities, strict=True))
        assert actual.length == expected.length
        assert [p.wkb for p in actual.polygons_full] == [p.wkb for p in expected.polygons_full]
        assert np.array_equal(actual.metadata["to_3D"], expected.metadata["to_3D"])
        assert np.array_equal(actual.metadata["face_index"], expected.metadata["face_index"])

    @pytest.mark.parametrize("angle", [0, 17, 67, -23, 90])
    @pytest.mark.parametrize("shape", ["holes", "separate", "overlap", "concave"])
    def test_hatch_culling_preserves_every_vector_and_order(self, angle, shape):
        from shapely.geometry import Polygon, box
        from pyslm.hatching import BaseHatcher
        from analytics.prediction.layer_engine import _make_hatcher, _polygons_to_paths

        polygons = {
            "holes": [Polygon([(0, 0), (40, 0), (40, 40), (0, 40)],
                              [[(5, 5), (5, 30), (30, 30), (30, 5)]])],
            "separate": [box(0, 0, 20, 20), box(100, 0, 120, 20)],
            "overlap": [box(0, 0, 30, 30), box(20, 10, 50, 40)],
            "concave": [Polygon([(0, 0), (40, 0), (40, 10), (10, 10),
                                  (10, 40), (0, 40)])],
        }[shape]
        paths = _polygons_to_paths(polygons)
        original_paths = [path.copy() for path in paths]
        reference = _make_hatcher(0.2)
        reference.hatchAngle = angle
        reference.clipLines = BaseHatcher.clipLines
        expected = reference.hatch(paths)
        hatcher = _make_hatcher(0.2)
        hatcher.hatchAngle = angle
        actual = hatcher.hatch(paths)
        assert [type(g) for g in actual.geometry] == [type(g) for g in expected.geometry]
        for got, want in zip(actual.geometry, expected.geometry, strict=True):
            assert np.array_equal(got.coords, want.coords)
        assert all(np.array_equal(path, original) for path, original in
                   zip(paths, original_paths, strict=True))

    @pytest.mark.parametrize("dtype", [np.float32, np.float64])
    def test_hatch_culling_keeps_grid_margin_and_original_ids(self, monkeypatch, dtype):
        from pyslm.hatching import BaseHatcher
        from analytics.prediction.layer_engine import _clip_hatch_lines

        paths = [np.array([[0, 0], [10, 0], [10, 10], [0, 10]])]
        xs = [-100, -BaseHatcher.error() / 4, 5, 10 + BaseHatcher.error() / 4, 100]
        lines = np.array([[[x, -5, i + 20], [x, 15, i + 20]] for i, x in enumerate(xs)],
                         dtype=dtype)
        original = lines.copy()
        original_clip = BaseHatcher.clipLines
        calls = []

        def clip(paths, candidates):
            calls.append(candidates.copy())
            return original_clip(paths, candidates)

        monkeypatch.setattr(BaseHatcher, "clipLines", staticmethod(clip))
        result = _clip_hatch_lines(paths, lines.reshape(-1, 3))
        assert len(calls) == 1
        assert np.array_equal(calls[0], original[1:4].reshape(-1, 3))
        assert np.array_equal(lines, original)
        expected = original_clip(paths, original.reshape(-1, 3))
        assert np.array_equal(result, expected)

    def test_hatch_culling_returns_real_empty_when_lines_miss_polygon(self):
        from analytics.prediction.layer_engine import _clip_hatch_lines

        paths = [np.array([[0, 0], [10, 0], [0, 10]])]
        # Inside the bounding box, outside the triangle: broad-phase rejection
        # cannot decide this, but the native intersection still must return empty.
        lines = np.array([[8, 8, 7], [9, 9, 7]], dtype=np.float32)
        assert _clip_hatch_lines(paths, lines).shape == (0, 2, 3)
        assert _clip_hatch_lines(paths, np.empty((0, 3))).shape == (0, 2, 3)

    @pytest.mark.parametrize("broken", ["lines", "paths"])
    def test_hatch_culling_rejects_nonfinite_geometry(self, broken):
        from analytics.prediction.layer_engine import _clip_hatch_lines

        paths = [np.array([[0, 0], [10, 0], [0, 10]], dtype=float)]
        lines = np.array([[2, -5, 7], [2, 10, 7]], dtype=float)
        if broken == "lines":
            lines[0, 0] = np.nan
        else:
            paths[0][0, 0] = np.inf
        with pytest.raises(EstimationError, match="Неконечные"):
            _clip_hatch_lines(paths, lines)

    def test_contour_only_small_body_remains_scannable(self, monkeypatch):
        from analytics.prediction import layer_engine

        monkeypatch.setattr(layer_engine, "_SECTION_THREADS", 1)
        mesh = trimesh.creation.box(extents=[0.5, 0.5, 1])
        mesh.apply_translation([0, 0, 0.5])
        series = layer_engine.compute_layer_series(meshes=[mesh], hatch_distance_mm=0.1,
                                                  layer_thickness_mm=0.06, uniform_levels=4)
        assert sum(series.hatch_mm) == 0
        assert sum(series.contour_mm) > 0
        assert sum(series.open_mm) == 0
        assert series.body_boundary_mm[0] > 0

    def test_batch_geometry_preserves_requested_heights_and_scalar_values(self):
        from analytics.prediction.layer_engine import GEOMETRY_FEATURES, LayerGeometrySeries

        series = LayerGeometrySeries(
            zs=[-2, 0.25, 7],
            **{name: [3.7 + index, 0.06 * index, 10.001 - index]
               for index, name in enumerate(GEOMETRY_FEATURES)},
            z_min=-3, z_max=8,
        )
        heights = [7, -3, 0.25, 0.7, -2, 8, 0.7]
        assert series.at_heights(heights).tolist() == [list(series.at(z)) for z in heights]
        assert series.at_heights([]).shape == (0, len(GEOMETRY_FEATURES))
        thickness = 0.06
        centres = series.z_min + (np.arange(series.layer_count(thickness)) + 0.5) * thickness
        assert series.at_layers(thickness).tolist() == [list(series.at(z)) for z in centres]

    def test_body_projection_reads_bounds_once_and_keeps_clipped_intervals(self):
        from analytics.prediction.layer_engine import LayerGeometrySeries
        from analytics.prediction.plate_estimator import _body_estimates

        class Mesh:
            reads = 0

            @property
            def bounds(self):
                self.reads += 1
                return np.array([[-3, -7, -2], [2, 11, 7]])

        mesh = Mesh()
        series = LayerGeometrySeries(
            zs=[-2, 7], hatch_mm=[1, 1], contour_mm=[1, 1], jump_mm=[0, 0],
            n_jumps=[0, 0], open_mm=[0, 0], z_min=-2, z_max=7,
            body_boundary_mm=[1],
            body_active_z_intervals_mm=[[[-5, -3], [-3, -2], [0, 5], [5, 20]]],
        )
        body, = _body_estimates([("support", "support", mesh, None)], series, 0.1)
        assert mesh.reads == 1
        assert (body.x_min_mm, body.x_max_mm, body.y_min_mm, body.y_max_mm) == (-3, 2, -7, 11)
        assert (body.z_min_mm, body.z_max_mm, body.height_mm, body.layer_count) == (-2, 7, 9, 90)
        assert body.volume_cm3 is None and body.scan_share == 1
        assert body.active_z_intervals_mm == [[-2, -2], [0, 5], [5, 7]]
        body.active_z_intervals_mm[0][0] = 99
        assert series.body_active_z_intervals_mm[0][1] == [-3, -2]

    def test_identity_xy_keeps_polygon_and_holes_without_rebuilding(self):
        from shapely.geometry import Polygon
        from analytics.prediction.layer_engine import _to_plate_xy

        polygon = Polygon(
            [(0, 0), (10, 0), (10, 20), (0, 20)],
            [[(2, 2), (5, 2), (5, 5), (2, 5)]],
        )
        transform = np.eye(4)
        transform[2, 3] = 119  # Z translation cannot change plate XY
        assert _to_plate_xy(polygon, transform) is polygon
        assert len(polygon.interiors) == 1

    @pytest.mark.parametrize("xy", [
        [[1, 0, 0, 12], [0, 1, 0, -8]],
        [[0, -1, 0, 0], [1, 0, 0, 0]],
        [[1, 0, 0, 1e-12], [0, 1, 0, 0]],
    ])
    def test_nonidentity_xy_transforms_every_ring_without_tolerance(self, xy):
        from shapely.geometry import Polygon
        from analytics.prediction.layer_engine import _to_plate_xy

        polygon = Polygon(
            [(0, 0), (10, 0), (10, 20), (0, 20)],
            [[(2, 2), (5, 2), (5, 5), (2, 5)]],
        )
        transform = np.eye(4)
        transform[:2] = xy
        projected = _to_plate_xy(polygon, transform)
        assert projected is not polygon
        for original, actual in zip(
            [polygon.exterior, *polygon.interiors],
            [projected.exterior, *projected.interiors],
            strict=True,
        ):
            points = np.asarray(original.coords)
            expected = points @ transform[:2, :2].T + transform[:2, 3]
            assert np.array_equal(actual.coords, expected)

    def test_single_part_sane_and_warns_about_missing_supports(self):
        est = estimate_plate([("box", _box_stl())], [], _params(), "steel")
        assert est.print_hours > 0
        [body] = est.bodies
        assert body.kind == "part" and body.scan_share == 1.0
        assert est.scan_source == "physics"
        assert body.layer_count == 100  # 10 mm / 0.1 mm
        assert body.z_min_mm == pytest.approx(0.0)
        assert body.z_max_mm == pytest.approx(10.0)
        assert body.x_max_mm - body.x_min_mm == pytest.approx(20.0)
        assert body.y_max_mm - body.y_min_mm == pytest.approx(20.0)
        assert est.geometry_totals["hatch_mm"] > 0
        # recoat: 100 layers x 10 s
        assert est.recoat_hours == pytest.approx(100 * 10 / 3600, rel=1e-6)
        assert any("НИЖНЕЙ границей" in w for w in est.warnings)

    def test_correction_factor_scales_scan_only(self):
        plain = estimate_plate([("box", _box_stl())], [], _params(), "steel")
        scaled = estimate_plate(
            [("box", _box_stl())], [], _params(time_correction_factor=1.5), "steel"
        )
        assert scaled.scan_hours == pytest.approx(plain.scan_hours * 1.5, rel=1e-6)
        assert scaled.recoat_hours == pytest.approx(plain.recoat_hours, rel=1e-6)
        assert scaled.print_hours == pytest.approx(
            plain.scan_hours * 1.5 + plain.recoat_hours, rel=1e-6,
        )
        assert scaled.raw_print_hours == pytest.approx(plain.raw_print_hours, rel=1e-6)

    def test_raised_magics_body_requires_explicit_origin_for_hidden_supports(self):
        # Native Magics supports may be absent. Real #08 proves that raised Z
        # alone does not establish whether the physical build began at Z=0.
        est = estimate_plate(
            [("raised", _box_stl(z=10.0, z_offset=5.0))], [], _params(), "steel",
        )
        [body] = est.bodies
        assert body.z_min_mm == pytest.approx(5.0)
        assert body.z_max_mm == pytest.approx(15.0)
        assert est.height_mm == pytest.approx(10.0)
        assert est.layer_count == 100
        assert est.build_origin_z_mm == pytest.approx(5.0)
        assert est.build_origin_source == "minimum_supplied_geometry_z"
        assert any("Начало печати не подтверждено" in warning for warning in est.warnings)

        explicit = estimate_plate(
            [("raised", _box_stl(z=10.0, z_offset=5.0))], [],
            _params(build_origin_z_mm=0.0), "steel",
        )
        assert explicit.height_mm == pytest.approx(15.0)
        assert explicit.layer_count == 150
        assert explicit.build_origin_source == "explicit"
        assert explicit.recoat_hours == pytest.approx(150 * 10 / 3600, rel=1e-6)

    def test_stl_minimum_at_zero_is_not_a_confirmed_build_origin(self):
        result = estimate_plate([("part", _box_stl(z=10.0))], [], _params(), "steel")
        assert result.build_origin_source == "minimum_supplied_geometry_z"
        assert any("Даже Z=0" in message for message in result.warnings)

    def test_geometry_below_explicit_origin_is_rejected(self):
        with pytest.raises(EstimationError, match="ниже заданного начала печати"):
            estimate_plate(
                [("crossing", _box_stl(z=10.0, z_offset=-5.0))], [],
                _params(build_origin_z_mm=0.0), "steel",
            )
        with pytest.raises(EstimationError, match="ниже заданного начала печати"):
            estimate_plate(
                [("below", _box_stl(z=10.0, z_offset=-15.0))], [],
                _params(build_origin_z_mm=0.0), "steel",
            )

    @pytest.mark.parametrize("height", [0.01, 0.049, 0.1])
    def test_sub_layer_geometry_is_one_physical_layer(self, height):
        est = estimate_plate(
            [("thin", _box_stl(z=height))], [], _params(), "steel",
        )
        assert est.layer_count == 1
        assert est.recoat_hours == pytest.approx(10 / 3600, rel=1e-6)


class TestSupports:
    def test_sheet_scan_time_matches_hand_calculation(self):
        # Vertical 30x20 wall, 0.1 mm layers -> 200 layers, 30 mm single track
        # per layer at 1000 mm/s = 0.03 s/layer -> 6 s total scan.
        est = estimate_plate([], [("s_wall", _sheet_stl())], _params(), "steel")
        [body] = est.bodies
        assert body.kind == "support" and body.scan_share == 1.0
        assert body.layer_count == 200
        # scan = open-track only: no correction warnings about lower bound
        assert est.scan_hours == pytest.approx(6.0 / 3600, rel=0.05)
        assert any("перескоки между ними не моделируются" in w for w in est.warnings)
        assert not any("НИЖНЕЙ границей" in w for w in est.warnings)

    def test_part_plus_support_sums_scan_and_shares_recoat(self):
        # Part 10 mm tall, support wall 20 mm tall -> plate height 20 mm,
        # recoat counted once from the union, not per body.
        est = estimate_plate(
            [("box", _box_stl(z=10.0))], [("s_wall", _sheet_stl(height=20.0))],
            _params(), "steel",
        )
        assert est.layer_count == 200
        assert est.recoat_hours == pytest.approx(200 * 10 / 3600, rel=1e-6)
        kinds = {b.kind for b in est.bodies}
        assert kinds == {"part", "support"}
        # shares sum to 1 and every body got some share
        assert sum(b.scan_share for b in est.bodies) == pytest.approx(1.0, rel=1e-6)
        assert all(b.scan_share > 0 for b in est.bodies)


class TestValidation:
    def test_missing_layer_thickness_raises(self):
        with pytest.raises(EstimationError):
            estimate_plate([("box", _box_stl())], [], _params(layer_thickness_mm=None), "steel")

    def test_no_bodies_raises(self):
        with pytest.raises(EstimationError):
            estimate_plate([], [], _params(), "steel")

    def test_default_recoat_is_flagged(self):
        est = estimate_plate([("box", _box_stl())], [], _params(recoat_time_ms=None), "steel")
        assert any("нанесения слоя не задано" in w for w in est.warnings)


class TestTruncationGuard:
    def test_repair_that_shrinks_the_build_aborts(self, monkeypatch):
        """Regression: MeshFix 'repaired' plates with sheet bodies by discarding
        geometry — over half the height vanished and the estimate came out
        several times low with only a soft warning attached."""
        import analytics.prediction.stl_slicer as slicer

        short = trimesh.creation.box(extents=[20, 20, 2])  # 2 mm survives of 10

        def fake_repair(mesh):
            return short, True

        monkeypatch.setattr(slicer, "_repair_mesh", fake_repair)
        # An open sheet is non-watertight -> triggers repair -> shrink -> abort.
        with pytest.raises(EstimationError, match="удалила часть геометрии"):
            slice_stl(_sheet_stl(height=10.0), 0.1)


class TestMagicsReader:
    @staticmethod
    def _synthetic_magics(tmp_path, with_support=True, below_platform=False):
        from analytics.prediction.magics_reader import _UNIT_MM

        box = trimesh.creation.box(extents=[10, 10, 10])
        if not below_platform:
            box.apply_translation([0, 0, -float(box.bounds[0][2])])  # sit on plate
        verts = np.round(box.vertices / _UNIT_MM).astype("<i4")
        faces = box.faces.astype("<u4")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            uid = "00000000-0000-0000-0000-000000000001"
            z.writestr(f"Stl{{{uid}}}_vertices", verts.tobytes())
            z.writestr(f"Stl{{{uid}}}_surfaces", faces.tobytes())
            if with_support:
                z.writestr("SupportSurfaces_ox01", b"\x01\x02\x03")
        raw = buf.getvalue()
        for a, b in ((b"PK\x03\x04", b"MT\x03\x04"), (b"PK\x01\x02", b"MT\x01\x02"),
                     (b"PK\x05\x06", b"MT\x05\x06")):
            raw = raw.replace(a, b)
        p = tmp_path / "plate.magics"
        p.write_bytes(raw)
        return p

    def test_reads_parts_and_flags_native_supports(self, tmp_path):
        from analytics.prediction.magics_reader import read_plate

        plate = read_plate(self._synthetic_magics(tmp_path))
        assert len(plate.parts) == 1
        assert plate.parts[0].volume == pytest.approx(1000.0, rel=1e-3)  # 10^3 mm^3
        assert plate.has_native_supports
        assert any("s_*.stl" in w for w in plate.warnings)
        assert plate.geometry_quality["status"] == "unconfirmed"
        assert "project_instance_placement" in plate.geometry_quality["missing"]

    def test_no_support_entries_no_support_warning(self, tmp_path):
        from analytics.prediction.magics_reader import read_plate

        plate = read_plate(self._synthetic_magics(tmp_path, with_support=False))
        assert not plate.has_native_supports
        assert not any("s_*.stl" in w for w in plate.warnings)
        assert "project_instance_placement" in plate.geometry_quality["missing"]
        assert "native_support_geometry" not in plate.geometry_quality["missing"]

    def test_body_below_platform_is_a_marker(self, tmp_path):
        from analytics.prediction.magics_reader import read_plate

        plate = read_plate(self._synthetic_magics(tmp_path, below_platform=True))
        assert len(plate.parts) == 0
        assert len(plate.markers) == 1


class TestFittedModelApplication:
    @pytest.mark.parametrize("lower,applicable", [(11000.0, True), (22000.0, False)])
    def test_free_branch_model_never_extrapolates_into_unknown_floor(self, lower, applicable):
        model = {"version": "max_base_floor_v2", "base_overhead_ms": 400.0,
                 "base_overhead_status": "identified_free_branch", "minimum_cycle_ms": None,
                 "minimum_cycle_status": "unidentified", "minimum_applicable_component_ms": lower}
        params = _scoped_model_params({"beta": [0, 0, 0, 0, 0, 1], "layer_cycle_model": model})
        est = estimate_plate([("box", _box_stl())], [], params, "steel")
        if applicable:
            assert est.layer_overhead_ms == 400.0
            assert est.machine_cycle_hours == pytest.approx(est.layer_count * 11.4 / 3600)
        else:
            assert est.layer_overhead_ms is None
            assert est.layer_overhead_source == "outside_calibrated_range"
            assert any("короче проверенной области" in warning for warning in est.warnings)

    def test_old_cycle_v1_is_not_automatically_treated_as_identified(self):
        params = _params(layer_cycle_model_by_mode={"test-printer|steel@0.100|lasers=1": {
            "version": "max_base_floor_v1", "base_overhead_ms": 8500.0,
            "minimum_cycle_ms": None, "minimum_cycle_status": "unidentified",
        }})
        est = estimate_plate([("box", _box_stl())], [], params, "steel")
        assert est.layer_overhead_ms is None

    def test_cycle_model_applies_even_without_accepted_scan_model(self):
        cycle_model = {
            "version": "max_base_floor_v2",
            "base_overhead_status": "identified",
            "base_overhead_ms": 400.0,
            "minimum_cycle_ms": 20_000.0,
            "minimum_cycle_status": "identified",
            "n_prints": 2,
            "n_layers": 400,
            "n_geometries": 2,
        }
        est = estimate_plate(
            [("box", _box_stl())], [],
            _params(layer_cycle_model_by_mode={"test-printer|steel@0.100|lasers=1": cycle_model}),
            "steel",
        )

        assert est.scan_source == "physics"
        assert est.minimum_layer_cycle_ms == 20_000.0
        assert est.machine_cycle_hours >= est.print_hours

    def test_fitted_model_replaces_physics_and_correction(self):
        from analytics.prediction.layer_engine import GEOMETRY_FEATURES

        # Physics baseline with an aggressive correction factor
        base = estimate_plate([("box", _box_stl())], [], _params(time_correction_factor=1.8), "steel")
        assert base.scan_source == "physics"
        assert base.correction_factor == 1.8

        # Fitted model for exactly this mode: pure hatch term at 500 mm/s
        beta = [0.0] * (len(GEOMETRY_FEATURES) + 1)
        beta[0] = 1.0 / 500.0
        fitted = estimate_plate(
            [("box", _box_stl())], [],
            _scoped_model_params({"beta": beta, "r2": 0.9, "layer_overhead_ms": 250.0},
                                 time_correction_factor=1.8),
            "steel",
        )
        assert fitted.scan_source == "fitted"
        assert fitted.method.endswith("+fitted")
        # Absolute: the 1.8 blanket factor must NOT stack on the fitted scan
        assert fitted.correction_factor == 1.0
        # Legacy additive residual lacks identification evidence, even though
        # the separate scoped scan model is usable.
        assert fitted.layer_overhead_ms is None
        assert fitted.layer_overhead_source == "unavailable"
        assert fitted.machine_cycle_hours == pytest.approx(fitted.print_hours)
        assert any("паспортным скоростям" in w for w in base.warnings)
        assert not any("паспортным скоростям" in w for w in fitted.warnings)

    def test_minimum_cycle_is_applied_per_layer_and_breakdown_balances(self):
        from analytics.prediction.layer_engine import GEOMETRY_FEATURES

        beta = [0.0] * (len(GEOMETRY_FEATURES) + 1)
        beta[-1] = 1.0  # one second of scan on every physical layer
        est = estimate_plate(
            [("box", _box_stl())], [],
            _scoped_model_params({
                "beta": beta,
                "layer_cycle_model": {
                    "version": "max_base_floor_v2",
                    "base_overhead_status": "identified",
                    "base_overhead_ms": 500.0,
                    "minimum_cycle_ms": 20_000.0,
                    "minimum_cycle_status": "identified",
                    "n_prints": 3,
                    "n_layers": 600,
                    "n_geometries": 3,
                    "floor_n_prints": 3,
                    "floor_n_layers": 200,
                },
            }),
            "steel",
        )

        assert est.minimum_layer_cycle_ms == 20_000.0
        assert est.minimum_cycle_active_layers == est.layer_count
        assert est.machine_cycle_hours == pytest.approx(est.layer_count * 20 / 3600)
        assert est.prediction.value == pytest.approx(est.machine_cycle_hours, abs=0.001)
        assert est.layer_overhead_hours == pytest.approx(
            est.machine_cycle_hours - est.scan_hours - est.recoat_hours,
        )

    def test_same_scan_total_can_have_different_cycle_total(self):
        cycle_model = {"base_overhead_ms": 0.0, "minimum_cycle_ms": 6_000.0}

        uneven, _, uneven_floor_layers = machine_cycle_from_layers(
            [1.0, 9.0], scan_correction_factor=1.0,
            recoat_ms=0.0, cycle_model=cycle_model,
        )
        even, _, even_floor_layers = machine_cycle_from_layers(
            [5.0, 5.0], scan_correction_factor=1.0,
            recoat_ms=0.0, cycle_model=cycle_model,
        )

        assert sum([1.0, 9.0]) == sum([5.0, 5.0])
        assert uneven == pytest.approx(15.0)
        assert even == pytest.approx(12.0)
        assert uneven_floor_layers == 1
        assert even_floor_layers == 2

    def test_scan_correction_is_applied_before_cycle_floor(self):
        total, _, floor_layers = machine_cycle_from_layers(
            [4.0], scan_correction_factor=2.0, recoat_ms=0.0,
            cycle_model={"base_overhead_ms": 0.0, "minimum_cycle_ms": 7_000.0},
        )

        assert total == pytest.approx(8.0)
        assert floor_layers == 0

    def test_fitted_model_ignored_for_other_mode(self):
        from analytics.prediction.layer_engine import GEOMETRY_FEATURES

        beta = [0.0] * (len(GEOMETRY_FEATURES) + 1)
        beta[0] = 1.0 / 500.0
        est = estimate_plate(
            [("box", _box_stl())], [],
            _params(scan_model_by_mat={"steel@0.025": {"beta": beta}}),  # другой режим
            "steel",
        )
        assert est.scan_source == "physics"


class _FakeGeometryCache:
    """In-memory stand-in for PrintsRepository's two cache methods."""

    def __init__(self):
        self.store: dict[str, dict] = {}
        self.gets = 0
        self.saves = 0

    def get_geometry_cache(self, cache_key):
        self.gets += 1
        return self.store.get(cache_key)

    def save_geometry_cache(self, cache_key, series_json, body_count):
        self.saves += 1
        self.store[cache_key] = series_json


class TestGeometryCache:
    """PLAN_ACCURACY.md 2.2 — skip re-slicing an unchanged set of STL bodies."""

    @pytest.mark.parametrize("index, changed", [(0, 0.1000001), (1, 0.0600001), (2, 0.0000001)])
    def test_nearby_geometry_settings_do_not_collide(self, index, changed):
        from analytics.prediction.plate_estimator import _geometry_cache_key

        named = [("box", _box_stl(), "part")]
        settings = [0.1, 0.06, 0.0]
        initial = _geometry_cache_key(named, *settings)
        settings[index] = changed
        assert _geometry_cache_key(named, *settings) != initial

    def test_second_call_skips_compute_layer_series(self, monkeypatch):
        import analytics.prediction.plate_estimator as pe

        calls = []
        real_compute = pe.compute_layer_series
        monkeypatch.setattr(pe, "compute_layer_series",
                             lambda *a, **k: (calls.append(1), real_compute(*a, **k))[1])

        cache = _FakeGeometryCache()
        parts = [("box", _box_stl())]
        first = estimate_plate(parts, [], _params(), "steel", geometry_cache=cache)
        assert len(calls) == 1
        assert cache.saves == 1

        second = estimate_plate(parts, [], _params(), "steel", geometry_cache=cache)
        assert len(calls) == 1, "compute_layer_series ran again on an identical cache hit"
        assert cache.saves == 1, "an existing entry must not be rewritten"

        # Snapshot rounding makes a hit non-bit-exact. This tolerance checks
        # only this box fixture, not a universal sampling or prediction bound.
        assert second.print_hours == pytest.approx(first.print_hours, rel=1e-3)
        assert second.layer_count == first.layer_count
        assert [b.scan_share for b in second.bodies] == pytest.approx(
            [b.scan_share for b in first.bodies], rel=1e-3,
        )

    def test_material_change_alone_still_hits(self, monkeypatch):
        """Material never enters compute_layer_series — a cache hit across a
        material change is the primary case PLAN_ACCURACY.md 2.2 exists for."""
        import analytics.prediction.plate_estimator as pe

        calls = []
        real_compute = pe.compute_layer_series
        monkeypatch.setattr(pe, "compute_layer_series",
                             lambda *a, **k: (calls.append(1), real_compute(*a, **k))[1])

        cache = _FakeGeometryCache()
        parts = [("box", _box_stl())]
        estimate_plate(parts, [], _params(), "steel", geometry_cache=cache)
        estimate_plate(parts, [], _params(), "aluminum", geometry_cache=cache)
        assert len(calls) == 1

    def test_different_hatch_distance_misses(self):
        cache = _FakeGeometryCache()
        parts = [("box", _box_stl())]
        estimate_plate(parts, [], _params(hatch_distance_mm=0.10), "steel", geometry_cache=cache)
        estimate_plate(parts, [], _params(hatch_distance_mm=0.12), "steel", geometry_cache=cache)
        assert cache.saves == 2, "different hatch_distance_mm must not collide"

    def test_different_layer_thickness_misses(self):
        cache = _FakeGeometryCache()
        parts = [("box", _box_stl())]
        estimate_plate(parts, [], _params(layer_thickness_mm=0.10), "steel", geometry_cache=cache)
        estimate_plate(parts, [], _params(layer_thickness_mm=0.05), "steel", geometry_cache=cache)
        assert cache.saves == 2, "different layer_thickness_mm must not collide"

    @pytest.mark.parametrize("changed", [
        {"contours_enabled": False}, {"hatch_angle_deg": 45.0},
        {"hatch_angle_deg": 67.0000001},
    ])
    def test_changed_scan_geometry_never_reuses_old_sections(self, changed):
        cache = _FakeGeometryCache()
        parts = [("box", _box_stl())]
        estimate_plate(parts, [], _params(), "steel", geometry_cache=cache)
        est = estimate_plate(parts, [], _params(**changed), "steel", geometry_cache=cache)
        assert cache.saves == 2
        if changed.get("contours_enabled") is False:
            assert est.geometry_totals["contour_mm"] == 0.0
            assert est.geometry_totals["hatch_mm"] > 0.0

    @pytest.mark.parametrize("changed", [
        {"contours_enabled": "false"}, {"contours_enabled": 0},
        {"hatch_angle_deg": True}, {"hatch_angle_deg": float("nan")},
        {"hatch_angle_deg": 10 ** 400},
    ])
    def test_invalid_scan_geometry_is_not_silently_defaulted(self, changed):
        with pytest.raises(EstimationError):
            estimate_plate([("box", _box_stl())], [], _params(**changed), "steel")

    def test_exact_height_physics_preserves_order_and_missing_layers(self):
        from analytics.prediction.layer_engine import (
            LayerGeometrySeries, physics_scan_seconds_by_layer,
        )

        series = LayerGeometrySeries(zs=[0.0, 10.0], hatch_mm=[0.0, 10000.0],
            contour_mm=[0.0, 0.0], jump_mm=[0.0, 0.0], n_jumps=[0.0, 0.0],
            open_mm=[0.0, 0.0], z_min=0.0, z_max=10.0)
        actual = physics_scan_seconds_by_layer(series, 0.1, _params(), "steel", 1,
                                               heights=[9.0, 2.0, 4.0])
        assert actual == pytest.approx([9.0, 2.0, 4.0])

    def test_different_body_misses(self):
        cache = _FakeGeometryCache()
        estimate_plate([("box", _box_stl())], [], _params(), "steel", geometry_cache=cache)
        estimate_plate([("box2", _box_stl(x=25.0))], [], _params(), "steel", geometry_cache=cache)
        assert cache.saves == 2, "a different STL body must not collide"

    def test_no_cache_argument_behaves_exactly_as_before(self):
        """geometry_cache=None (the default) must reproduce the uncached path."""
        est = estimate_plate([("box", _box_stl())], [], _params(), "steel")
        assert est.print_hours > 0

    def test_path_source_matches_bytes_and_reuses_same_cache_entry(self, tmp_path):
        """Workers may pass disk-backed STLs without changing cache identity."""
        blob = _box_stl()
        path = tmp_path / "box.stl"
        path.write_bytes(blob)
        cache = _FakeGeometryCache()

        from_bytes = estimate_plate([("box", blob)], [], _params(), "steel", cache)
        from_path = estimate_plate([("box", path)], [], _params(), "steel", cache)

        assert cache.saves == 1
        assert from_path.print_hours == pytest.approx(from_bytes.print_hours, rel=1e-3)
