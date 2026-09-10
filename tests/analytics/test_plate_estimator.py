"""Plate estimator: parts via PySLM, supports via sections, honest warnings."""
import io
import zipfile

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("shapely")

from analytics.prediction.plate_estimator import (  # noqa: E402
    _machine_cycle_from_layers,
    estimate_plate,
)
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


class TestParts:
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

    def test_no_support_entries_no_support_warning(self, tmp_path):
        from analytics.prediction.magics_reader import read_plate

        plate = read_plate(self._synthetic_magics(tmp_path, with_support=False))
        assert not plate.has_native_supports
        assert not any("s_*.stl" in w for w in plate.warnings)

    def test_body_below_platform_is_a_marker(self, tmp_path):
        from analytics.prediction.magics_reader import read_plate

        plate = read_plate(self._synthetic_magics(tmp_path, below_platform=True))
        assert len(plate.parts) == 0
        assert len(plate.markers) == 1


class TestFittedModelApplication:
    def test_cycle_model_applies_even_without_accepted_scan_model(self):
        cycle_model = {
            "version": "max_base_floor_v1",
            "base_overhead_ms": 400.0,
            "minimum_cycle_ms": 20_000.0,
            "minimum_cycle_status": "identified",
            "n_prints": 2,
            "n_layers": 400,
            "n_geometries": 2,
        }
        est = estimate_plate(
            [("box", _box_stl())], [],
            _params(layer_cycle_model_by_mode={"steel@0.100": cycle_model}),
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
            _params(time_correction_factor=1.8,
                    scan_model_by_mat={"steel@0.100": {
                        "beta": beta, "r2": 0.9, "layer_overhead_ms": 250.0,
                    }}),
            "steel",
        )
        assert fitted.scan_source == "fitted"
        assert fitted.method.endswith("+fitted")
        # Absolute: the 1.8 blanket factor must NOT stack on the fitted scan
        assert fitted.correction_factor == 1.0
        assert fitted.layer_overhead_ms == 250.0
        assert fitted.layer_overhead_source == "legacy_additive_overhead"
        assert fitted.machine_cycle_hours == pytest.approx(
            fitted.print_hours + fitted.layer_count * 0.25 / 3600,
        )
        assert any("паспортным скоростям" in w for w in base.warnings)
        assert not any("паспортным скоростям" in w for w in fitted.warnings)

    def test_minimum_cycle_is_applied_per_layer_and_breakdown_balances(self):
        from analytics.prediction.layer_engine import GEOMETRY_FEATURES

        beta = [0.0] * (len(GEOMETRY_FEATURES) + 1)
        beta[-1] = 1.0  # one second of scan on every physical layer
        est = estimate_plate(
            [("box", _box_stl())], [],
            _params(scan_model_by_mat={"steel@0.100": {
                "beta": beta,
                "layer_cycle_model": {
                    "version": "max_base_floor_v1",
                    "base_overhead_ms": 500.0,
                    "minimum_cycle_ms": 20_000.0,
                    "minimum_cycle_status": "identified",
                    "n_prints": 3,
                    "n_layers": 600,
                    "n_geometries": 3,
                    "floor_n_prints": 3,
                    "floor_n_layers": 200,
                },
            }}),
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

        uneven, _, uneven_floor_layers = _machine_cycle_from_layers(
            [1.0, 9.0], scan_correction_factor=1.0,
            recoat_ms=0.0, cycle_model=cycle_model,
        )
        even, _, even_floor_layers = _machine_cycle_from_layers(
            [5.0, 5.0], scan_correction_factor=1.0,
            recoat_ms=0.0, cycle_model=cycle_model,
        )

        assert sum([1.0, 9.0]) == sum([5.0, 5.0])
        assert uneven == pytest.approx(15.0)
        assert even == pytest.approx(12.0)
        assert uneven_floor_layers == 1
        assert even_floor_layers == 2

    def test_scan_correction_is_applied_before_cycle_floor(self):
        total, _, floor_layers = _machine_cycle_from_layers(
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

        # Numeric fidelity through to_snapshot()/from_snapshot() — not bit-exact,
        # since to_snapshot() rounds hatch_mm etc. to 1 decimal place for compact
        # storage (it was designed for calibration, which averages over
        # thousands of layers). A single geometry component can be off by up to
        # 0.1 mm against values in the thousands, i.e. rel~1e-4-1e-3 — far below
        # this project's accepted +-0.7% sampling-grid noise floor and totally
        # unlike the >=1% swings a real cache-key bug (wrong body, stale hatch
        # distance) would produce, which is what this bound is actually guarding.
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
