"""Tests for Phase 3: predicted-vs-actual, calibration, shifts (ruptures), LightGBM."""
import io
from datetime import datetime, timezone

import pytest
import trimesh
from fastapi.testclient import TestClient

from api.main import app
from domain.models.prints import PrintRecord
from domain.models.sessions import BuildSession
from storage.db.session import SessionLocal

client = TestClient(app)

CUBE_STL = trimesh.creation.box(extents=[10, 10, 10]).export(file_type="stl")


class TestAccuracyReport:
    def _seed_pair(self, db, idx: int, predicted: float, actual_hours: float) -> None:
        start = datetime(2028, 1, idx, 8, 0, tzinfo=timezone.utc)
        end = start.replace(hour=8 + int(actual_hours), minute=int((actual_hours % 1) * 60))
        sid = f"s_acc_{idx}"
        db.add(BuildSession(session_id=sid, status="x", context={}, start_ts=start, end_ts=end))
        db.add(PrintRecord(
            record_id=f"pr_acc_{idx}", name=f"acc{idx}", session_id=sid,
            metadata_json={"prediction": {
                "estimated_at": start.isoformat(),
                "fast": {"method": "excel", "print_hours": predicted * 3, "cost_total_rub": 1},
                "accurate": {"method": "pyslm", "print_hours": predicted, "cost_total_rub": 1},
            }},
        ))

    def test_report_and_suggested_factor(self):
        from analytics.prediction.accuracy import prediction_accuracy

        with SessionLocal() as db:
            # 3 пары: точный прогноз 2ч, факт 4ч → ratio 2.0
            for i in (1, 2, 3):
                self._seed_pair(db, i, predicted=2.0, actual_hours=4.0)
            db.flush()
            report = prediction_accuracy(db)
            db.rollback()

        assert report["n_pairs"] >= 3
        assert report["suggested_correction_factor"] == pytest.approx(2.0, abs=0.1)
        row = next(r for r in report["pairs"] if r["record_id"] == "pr_acc_1")
        assert row["actual_hours"] == pytest.approx(4.0, abs=0.1)
        assert row["accurate_error_pct"] == pytest.approx(-50.0, abs=2)

    def test_endpoint(self):
        r = client.get("/prints/prediction-accuracy")
        assert r.status_code == 200
        assert "suggested_correction_factor" in r.json()


class TestCorrectionFactor:
    def test_factor_scales_physics_time(self):
        from analytics.prediction.print_time import estimate_print_time
        from analytics.prediction.stl_slicer import slice_stl

        params = {
            "hatch_speed_mm_s": 1000.0, "contour_speed_mm_s": 500.0,
            "hatch_distance_mm": 0.1, "layer_thickness_mm": 0.05, "laser_count": 2,
            "recoat_time_ms": None, "material_densities": {}, "hatch_speeds_by_mat": {},
        }
        s = slice_stl(CUBE_STL, 0.05)
        base = estimate_print_time(s, params, "steel", mode="pyslm")
        doubled = estimate_print_time(s, {**params, "time_correction_factor": 2.0}, "steel", mode="pyslm")
        assert doubled.scan_hours == pytest.approx(base.scan_hours * 2, rel=0.01)
        # Excel-режим коэффициентом не трогаем — формула уже откалибрована оператором
        excel = estimate_print_time(s, {**params, "time_correction_factor": 2.0}, "steel", mode="excel")
        excel_base = estimate_print_time(s, params, "steel", mode="excel")
        assert excel.scan_hours == pytest.approx(excel_base.scan_hours, rel=0.001)


class TestEstimateRecordEndpoint:
    def test_estimate_stores_snapshot(self, monkeypatch):
        class _Store:
            data = {}
            def __init__(self, *a, **k): pass
            def is_available(self): return True
            def put_bytes(self, b, o, d, content_type=""):
                _Store.data[(b, o)] = d
                return f"s3://{b}/{o}"
            def get_bytes(self, b, o): return _Store.data.get((b, o))
            def remove_object(self, b, o): return _Store.data.pop((b, o), None) is not None

        monkeypatch.setattr("api.routes.prints.ObjectStore", _Store)
        client.put("/settings/machine", json={
            "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500, "hatch_distance_mm": 0.1,
            "layer_thickness_mm": 0.05, "laser_count": 2, "recoat_time_ms": 9000,
            "powder_cost_rub_per_kg": 7000, "material_densities": {"steel": 7.9},
        })
        rec = client.post("/prints", json={"name": "куб для прогноза"}).json()
        client.post(
            f"/prints/{rec['record_id']}/files",
            files={"file": ("cube.stl", io.BytesIO(CUBE_STL), "model/stl")},
            data={"file_type": "stl"},
        )
        r = client.post(f"/prints/{rec['record_id']}/estimate")
        assert r.status_code == 200
        snap = r.json()["prediction"]
        assert snap["fast"]["print_hours"] > 0
        assert snap["accurate"]["print_hours"] > 0
        stored = client.get(f"/prints/{rec['record_id']}").json()
        assert stored["metadata_json"]["prediction"]["fast"]["method"] == "excel"

    def test_estimate_without_stl_422(self):
        rec = client.post("/prints", json={"name": "без stl"}).json()
        assert client.post(f"/prints/{rec['record_id']}/estimate").status_code == 422

    def test_auto_estimate_on_stl_upload(self, monkeypatch):
        """Загрузка STL в карточку сама создаёт снапшот прогноза (фоновая задача)."""
        class _Store:
            data = {}
            def __init__(self, *a, **k): pass
            def is_available(self): return True
            def put_bytes(self, b, o, d, content_type=""):
                _Store.data[(b, o)] = d
                return f"s3://{b}/{o}"
            def get_bytes(self, b, o): return _Store.data.get((b, o))
            def remove_object(self, b, o): return _Store.data.pop((b, o), None) is not None

        monkeypatch.setattr("api.routes.prints.ObjectStore", _Store)
        client.put("/settings/machine", json={
            "hatch_speed_mm_s": 1000, "contour_speed_mm_s": 500, "hatch_distance_mm": 0.1,
            "layer_thickness_mm": 0.05, "laser_count": 2, "recoat_time_ms": 9000,
            "powder_cost_rub_per_kg": 7000, "material_densities": {"steel": 7.9},
        })
        rec = client.post("/prints", json={"name": "авто-прогноз"}).json()
        # TestClient выполняет background tasks до возврата ответа
        client.post(
            f"/prints/{rec['record_id']}/files",
            files={"file": ("part.stl", io.BytesIO(CUBE_STL), "model/stl")},
            data={"file_type": "stl"},
        )
        stored = client.get(f"/prints/{rec['record_id']}").json()
        pred = (stored["metadata_json"] or {}).get("prediction")
        assert pred is not None
        assert pred["fast"]["print_hours"] > 0
        assert pred["accurate"]["print_hours"] > 0

    def test_auto_estimate_skipped_without_params(self, monkeypatch):
        """Без параметров машины загрузка STL не падает — прогноз просто пропускается."""
        class _Store:
            data = {}
            def __init__(self, *a, **k): pass
            def is_available(self): return True
            def put_bytes(self, b, o, d, content_type=""):
                _Store.data[(b, o)] = d
                return f"s3://{b}/{o}"
            def get_bytes(self, b, o): return _Store.data.get((b, o))
            def remove_object(self, b, o): return _Store.data.pop((b, o), None) is not None

        monkeypatch.setattr("api.routes.prints.ObjectStore", _Store)
        client.put("/settings/machine", json={"hatch_speed_mm_s": None, "laser_count": None})
        rec = client.post("/prints", json={"name": "без параметров"}).json()
        r = client.post(
            f"/prints/{rec['record_id']}/files",
            files={"file": ("part.stl", io.BytesIO(CUBE_STL), "model/stl")},
            data={"file_type": "stl"},
        )
        assert r.status_code == 200  # загрузка успешна, авто-прогноз тихо пропущен
        stored = client.get(f"/prints/{rec['record_id']}").json()
        assert (stored["metadata_json"] or {}).get("prediction") is None


class TestShiftDetector:
    def _sessions(self, values, signal="SO1"):
        return [
            {
                "group_id": f"s{i}",
                "session_id": f"s{i}",
                "signal_stats": {signal: {"mean": v, "std": 0.1, "min": v, "max": v,
                                          "n": 100, "group": "oxygen"}},
            }
            for i, v in enumerate(values)
        ]

    def test_detects_step_change(self):
        from analytics.cross_session import detect_signal_shifts

        vals = [1.0, 1.02, 0.98, 1.01, 0.99, 1.0, 2.0, 2.02, 1.98, 2.01, 2.0, 1.99]
        shifts = detect_signal_shifts(self._sessions(vals))
        assert shifts, "step change must be detected"
        assert shifts[0]["signal"] == "SO1"
        assert shifts[0]["direction"] == "up"
        assert shifts[0]["jump_pct"] > 50

    def test_no_shift_on_stable_signal(self):
        from analytics.cross_session import detect_signal_shifts

        vals = [1.0, 1.01, 0.99, 1.0, 1.02, 0.98, 1.0, 1.01]
        assert detect_signal_shifts(self._sessions(vals)) == []

    def test_too_few_sessions(self):
        from analytics.cross_session import detect_signal_shifts

        assert detect_signal_shifts(self._sessions([1, 2, 3])) == []

    def test_included_in_combined_analysis(self):
        from analytics.cross_session import run_cross_session_analysis

        vals = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        out = run_cross_session_analysis(self._sessions(vals))
        assert "shifts" in out
        assert out["shifts"]


class TestLightGBMDefectModel:
    def _group(self, risk_level: float) -> dict:
        return {
            "features": {
                "atmosphere_readiness": 100 - risk_level * 60,
                "process_anomaly_count": risk_level * 4,
                "data_quality_score": 95.0,
                "duration_min": 120.0,
                "layers": 500.0,
            },
            "health": {"burn_drift": {"slope_sec_per_layer": risk_level * 0.4}},
            "signal_stats": {"SO1": {"mean": 0.5 + risk_level, "std": 0.05 + risk_level / 10}},
        }

    def test_gbm_trained_with_enough_labels(self):
        from analytics.prediction.defect_risk import predict_defect_risk, train_defect_model

        data = [(self._group(0.1), 0) for _ in range(12)] + [(self._group(0.9), 1) for _ in range(12)]
        model = train_defect_model(data)
        assert model is not None
        assert model["type"] == "lightgbm"

        risky = predict_defect_risk(self._group(0.9), model)
        safe = predict_defect_risk(self._group(0.1), model)
        assert risky["method"] == "lightgbm"
        assert risky["risk"] > safe["risk"]

    def test_logreg_with_medium_labels(self):
        from analytics.prediction.defect_risk import train_defect_model

        data = [(self._group(0.1), 0) for _ in range(5)] + [(self._group(0.9), 1) for _ in range(5)]
        model = train_defect_model(data)
        assert model is not None
        assert model["type"] == "logreg"
