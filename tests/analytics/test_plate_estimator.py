"""Plate estimator: parts via PySLM, supports via sections, honest warnings."""
import io
import zipfile

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("shapely")

from analytics.prediction.plate_estimator import estimate_plate  # noqa: E402
from analytics.prediction.stl_slicer import EstimationError, slice_stl  # noqa: E402


def _box_stl(x=20.0, y=20.0, z=10.0) -> bytes:
    box = trimesh.creation.box(extents=[x, y, z])
    box.apply_translation([0, 0, -float(box.bounds[0][2])])  # sit on the plate
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
        # recoat: 100 layers x 10 s
        assert est.recoat_hours == pytest.approx(100 * 10 / 3600, rel=1e-6)
        assert any("НИЖНЕЙ границей" in w for w in est.warnings)

    def test_correction_factor_scales_total_once(self):
        plain = estimate_plate([("box", _box_stl())], [], _params(), "steel")
        scaled = estimate_plate(
            [("box", _box_stl())], [], _params(time_correction_factor=1.5), "steel"
        )
        assert scaled.print_hours == pytest.approx(plain.print_hours * 1.5, rel=1e-6)
        assert scaled.raw_print_hours == pytest.approx(plain.raw_print_hours, rel=1e-6)


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
                    scan_model_by_mat={"steel@0.100": {"beta": beta, "r2": 0.9}}),
            "steel",
        )
        assert fitted.scan_source == "fitted"
        assert fitted.method.endswith("+fitted")
        # Absolute: the 1.8 blanket factor must NOT stack on the fitted scan
        assert fitted.correction_factor == 1.0
        assert any("паспортным скоростям" in w for w in base.warnings)
        assert not any("паспортным скоростям" in w for w in fitted.warnings)

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
