"""Tests for Phase 1: STL slicing, print-time and cost estimation."""
import io

import pytest
import trimesh
from fastapi.testclient import TestClient

from analytics.prediction.cost_estimator import estimate_cost
from analytics.prediction.print_time import EstimationError, estimate_print_time
from analytics.prediction.stl_slicer import slice_stl

CUBE_STL = trimesh.creation.box(extents=[10, 10, 10]).export(file_type="stl")

PARAMS = {
    "hatch_speed_mm_s": 1000.0,
    "contour_speed_mm_s": 500.0,
    "hatch_distance_mm": 0.1,
    "layer_thickness_mm": 0.05,
    "laser_count": 2,
    "recoat_time_ms": 9000.0,
    "powder_cost_rub_per_kg": 7000.0,
    "gas_cost_rub_per_atm": 50.0,
    "gas_atm_per_print": 10.0,
    "filter_cost_rub": 15000.0,
    "filter_lifetime_hours": 500.0,
    "platform_cost_rub": 2000.0,
    "material_densities": {"steel": 7.9},
    "hatch_speeds_by_mat": {},
}


class TestSlicer:
    def test_cube_geometry(self):
        s = slice_stl(CUBE_STL, 0.05)
        assert s.volume_mm3 == pytest.approx(1000.0, rel=1e-3)
        assert s.height_mm == pytest.approx(10.0)
        assert s.layer_count == 200
        assert s.avg_area_mm2 == pytest.approx(100.0, rel=1e-3)
        assert s.avg_perimeter_mm == pytest.approx(40.0, rel=1e-3)
        assert s.is_watertight
        assert not s.was_repaired  # целая сетка не трогается

    def test_holed_mesh_auto_repaired(self):
        # Куб без одной грани (2 треугольника) — негерметичный, объём занижен
        cube = trimesh.creation.box(extents=[10, 10, 10])
        holed = trimesh.Trimesh(vertices=cube.vertices, faces=cube.faces[2:], process=True)
        assert not holed.is_watertight
        s = slice_stl(holed.export(file_type="stl"), 0.05)
        assert s.was_repaired
        assert s.is_watertight
        # Починка восстановила недостающую грань → корректный объём
        assert s.volume_mm3 == pytest.approx(1000.0, rel=1e-2)
        assert any("починена" in w for w in s.warnings)

    def test_huge_holed_mesh_repair_skipped(self, monkeypatch):
        # Защита от зависания: большая негерметичная сетка не чинится
        import analytics.prediction.stl_slicer as slicer

        monkeypatch.setattr(slicer, "_MAX_REPAIR_FACES", 2)  # куб = 12 граней > 2
        cube = trimesh.creation.box(extents=[10, 10, 10])
        holed = trimesh.Trimesh(vertices=cube.vertices, faces=cube.faces[2:], process=True)
        s = slice_stl(holed.export(file_type="stl"), 0.05)
        assert not s.was_repaired
        assert any("слишком большая" in w for w in s.warnings)

    def test_section_cap_for_tall_parts(self):
        tall = trimesh.creation.box(extents=[5, 5, 100]).export(file_type="stl")
        s = slice_stl(tall, 0.05)
        assert s.layer_count == 2000
        assert len(s.section_zs) == 400  # capped, not 2000

    def test_invalid_layer_thickness(self):
        with pytest.raises(ValueError):
            slice_stl(CUBE_STL, 0)

    def test_garbage_bytes_rejected(self):
        with pytest.raises(Exception):
            slice_stl(b"not an stl at all", 0.05)


class TestPrintTime:
    def test_excel_mode_reproduces_operator_spreadsheet(self):
        """Точные входные из «расчёт стоимости Cталь.xlsx» → 40.36 ч."""
        from analytics.prediction.stl_slicer import SliceResult

        s = SliceResult(
            volume_mm3=132000, height_mm=60, layer_count=2000, layer_thickness_mm=0.03,
            section_zs=[0.015], section_areas_mm2=[2270.0], section_perimeters_mm=[190.578],
        )
        params = {**PARAMS, "hatch_distance_mm": 0.12, "laser_count": 1,
                  "contour_speed_mm_s": 430.0, "recoat_time_ms": None}
        t = estimate_print_time(s, params, "steel", mode="excel")
        assert t.method == "excel"
        assert t.scan_hours == pytest.approx(40.36, abs=0.01)

    def test_excel_mode_cube_math(self):
        # t_слоя = (√100/0.1)×(4√100)/1000×0.96 = 100×40/1000×0.96 = 3.84 c
        s = slice_stl(CUBE_STL, 0.05)
        t = estimate_print_time(s, PARAMS, "steel", mode="excel")
        expected_scan_h = 3.84 * 200 / 2 / 3600
        assert t.scan_hours == pytest.approx(expected_scan_h, rel=0.01)
        assert t.recoat_hours == pytest.approx(200 * 9.0 / 3600, abs=0.01)

    def test_pyslm_mode_uses_real_vectors(self):
        s = slice_stl(CUBE_STL, 0.05)
        t = estimate_print_time(s, PARAMS, "steel", stl_bytes=CUBE_STL, mode="pyslm")
        assert t.method == "pyslm"
        # Физика: ~(100/0.1)/1000 + 40/500 = 1.08 c/слой → ×200/2 = 108 c
        assert t.scan_hours == pytest.approx(108 / 3600, rel=0.35)

    def test_pyslm_mode_without_bytes_degrades_to_physics(self):
        s = slice_stl(CUBE_STL, 0.05)
        t = estimate_print_time(s, PARAMS, "steel", mode="pyslm")  # без stl_bytes
        assert t.method == "physics"
        assert any("PySLM" in w for w in t.warnings)

    def test_material_hatch_speed_override(self):
        params = {**PARAMS, "hatch_speeds_by_mat": {"steel": 2000.0}}
        s = slice_stl(CUBE_STL, 0.05)
        fast = estimate_print_time(s, params, "steel", mode="excel")
        slow = estimate_print_time(s, params, "aluminum", mode="excel")  # фоллбэк на 1000
        assert fast.scan_hours < slow.scan_hours

    def test_missing_speed_raises(self):
        s = slice_stl(CUBE_STL, 0.05)
        with pytest.raises(EstimationError):
            estimate_print_time(s, {**PARAMS, "hatch_speed_mm_s": None}, "steel")

    def test_missing_hatch_distance_raises(self):
        s = slice_stl(CUBE_STL, 0.05)
        with pytest.raises(EstimationError):
            estimate_print_time(s, {**PARAMS, "hatch_distance_mm": None}, "steel")

    def test_missing_lasers_raises(self):
        s = slice_stl(CUBE_STL, 0.05)
        with pytest.raises(EstimationError):
            estimate_print_time(s, {**PARAMS, "laser_count": None}, "steel")

    def test_unknown_mode_raises(self):
        s = slice_stl(CUBE_STL, 0.05)
        with pytest.raises(EstimationError):
            estimate_print_time(s, PARAMS, "steel", mode="magic")

    def test_missing_recoat_warns(self):
        s = slice_stl(CUBE_STL, 0.05)
        t = estimate_print_time(s, {**PARAMS, "recoat_time_ms": None}, "steel", mode="excel")
        assert t.recoat_hours == 0
        assert any("recoat" in w for w in t.warnings)


class TestCost:
    def _time(self, params=PARAMS):
        s = slice_stl(CUBE_STL, 0.05)
        return s, estimate_print_time(s, params, "steel", mode="excel")

    def test_all_items_present(self):
        s, t = self._time()
        c = estimate_cost(s, PARAMS, "steel", t)
        # порошок: 1 см³ × 7.9 г/см³ = 0.0079 кг × 7000 ≈ 55.3 ₽
        assert c.powder_kg == pytest.approx(0.008, abs=0.001)
        assert c.breakdown["порошок"] == pytest.approx(55.3, abs=0.5)
        assert c.breakdown["газ"] == 500.0
        assert c.breakdown["платформа"] == 2000.0
        assert c.total_rub == pytest.approx(sum(c.breakdown.values()), abs=0.01)
        assert not c.warnings

    def test_powder_cost_override(self):
        s, t = self._time()
        c = estimate_cost(s, PARAMS, "steel", t, powder_cost_override=14000.0)
        assert c.breakdown["порошок"] == pytest.approx(110.6, abs=1)

    def test_unknown_material_density_warns(self):
        s, t = self._time()
        c = estimate_cost(s, PARAMS, "inconel", t)
        assert c.powder_kg is None
        assert "порошок" not in c.breakdown
        assert any("плотность" in w for w in c.warnings)

    def test_missing_rates_warn_not_fail(self):
        s, t = self._time()
        params = {**PARAMS, "gas_cost_rub_per_atm": None, "filter_cost_rub": None, "platform_cost_rub": None}
        c = estimate_cost(s, params, "steel", t)
        assert set(c.breakdown) == {"порошок"}
        assert len(c.warnings) == 3


class TestStlEstimateEndpoint:
    @pytest.fixture
    def client(self):
        from api.main import app
        return TestClient(app)

    def test_prediction_available_when_configured(self, client):
        put = client.put("/settings/machine", json={
            "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500, "hatch_distance_mm": 0.1,
            "layer_thickness_mm": 0.05, "laser_count": 2, "recoat_time_ms": 9000,
            "powder_cost_rub_per_kg": 7000, "material_densities": {"steel": 7.9},
        })
        assert put.status_code == 200 and put.json()["configured"]

        fast = client.post(
            "/upload/stl-estimate?material=steel&method=fast",
            files={"file": ("cube.stl", io.BytesIO(CUBE_STL), "model/stl")},
        )
        assert fast.status_code == 200
        pred = fast.json()["prediction"]
        assert pred["available"] is True
        assert pred["layer_count"] == 200
        assert pred["print_hours"] > 0
        assert pred["method"] == "excel"
        assert pred["cost_breakdown"].get("порошок") is not None

        accurate = client.post(
            "/upload/stl-estimate?material=steel&method=accurate",
            files={"file": ("cube.stl", io.BytesIO(CUBE_STL), "model/stl")},
        )
        pred_acc = accurate.json()["prediction"]
        assert pred_acc["method"] in ("pyslm", "physics")
        assert pred_acc["print_hours"] > 0

    def test_invalid_method_rejected(self, client):
        r = client.post(
            "/upload/stl-estimate?method=guess",
            files={"file": ("cube.stl", io.BytesIO(CUBE_STL), "model/stl")},
        )
        assert r.status_code == 422

    def test_prediction_unavailable_without_params(self, client):
        # Сбросить критичные параметры
        client.put("/settings/machine", json={"hatch_speed_mm_s": None, "laser_count": None})
        r = client.post(
            "/upload/stl-estimate",
            files={"file": ("cube.stl", io.BytesIO(CUBE_STL), "model/stl")},
        )
        assert r.status_code == 200
        pred = r.json()["prediction"]
        assert pred["available"] is False
        assert "Заполните параметры" in pred["reason"]
