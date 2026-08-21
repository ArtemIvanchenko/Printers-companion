"""Tests for the unified analytics.prediction.contract.PredictionResult wiring.

Each predictor keeps its own existing result shape (checked in its own test
file); these tests only check that the additive ``prediction`` field/key is
populated with the right source/sample_size/explanation, and that nothing
existing broke.
"""
import pytest
import trimesh

from analytics.prediction.contract import PredictionResult, PredictionSource
from analytics.prediction.cost_estimator import estimate_cost
from analytics.prediction.defect_risk import predict_defect_risk, train_defect_model
from analytics.prediction.maintenance import MIN_SESSIONS, forecast_maintenance
from analytics.prediction.print_time import estimate_print_time
from analytics.prediction.stl_slicer import slice_stl

CUBE_STL = trimesh.creation.box(extents=[10, 10, 10]).export(file_type="stl")

BASE_PARAMS = {
    "hatch_speed_mm_s": 1000.0,
    "contour_speed_mm_s": 500.0,
    "hatch_distance_mm": 0.1,
    "layer_thickness_mm": 0.05,
    "laser_count": 2,
    "recoat_time_ms": 9000.0,
}


class TestPredictionResultContract:
    def test_to_dict_round_trips_interval(self):
        r = PredictionResult(
            value=1.5, unit="ч", source=PredictionSource.CALIBRATED,
            interval=(1.2, 1.8), sample_size=7, warnings=["w"], explanation="e",
        )
        d = r.to_dict()
        assert d == {
            "value": 1.5, "unit": "ч", "source": "calibrated",
            "interval": [1.2, 1.8], "sample_size": 7,
            "warnings": ["w"], "explanation": "e",
        }

    def test_to_dict_none_interval_stays_none(self):
        r = PredictionResult(value=1.0, unit="ч", source=PredictionSource.DEFAULT)
        assert r.to_dict()["interval"] is None


class TestPrintTimePrediction:
    def test_uncalibrated_physics_is_calculated(self):
        slices = slice_stl(CUBE_STL, 0.05)
        est = estimate_print_time(slices, BASE_PARAMS, "steel", stl_bytes=CUBE_STL)
        assert est.prediction is not None
        assert est.prediction.source == PredictionSource.CALCULATED
        assert est.prediction.value == pytest.approx(est.print_hours, abs=1e-3)
        assert est.prediction.unit == "ч"
        assert est.prediction.interval is None  # интервалы — отдельный шаг (п.2)

    def test_calibrated_physics_is_calibrated(self):
        params = {**BASE_PARAMS, "time_correction_by_mat": {"steel": 1.2}}
        slices = slice_stl(CUBE_STL, 0.05)
        est = estimate_print_time(slices, params, "steel", stl_bytes=CUBE_STL)
        assert est.prediction.source == PredictionSource.CALIBRATED
        assert "1.200" in est.prediction.explanation

    def test_fitted_model_is_model_source_with_sample_size(self):
        params = {
            **BASE_PARAMS,
            "scan_model_by_mat": {
                "steel@0.050": {
                    "beta": [0.001, 0.001, 0.0001, 0.0, 0.0, 0.0],
                    "n_layers": 842,
                    "r2": 0.91,
                }
            },
        }
        slices = slice_stl(CUBE_STL, 0.05)
        est = estimate_print_time(slices, params, "steel", stl_bytes=CUBE_STL)
        assert est.prediction.source == PredictionSource.MODEL
        assert est.prediction.sample_size == 842
        assert "0.91" in est.prediction.explanation


class TestCostPrediction:
    def _params(self, **over):
        base = dict(BASE_PARAMS, material_densities={"steel": 7.9},
                    powder_cost_rub_per_kg=7000.0, gas_cost_rub_per_atm=50.0,
                    gas_atm_per_print=10.0, filter_cost_rub=15000.0,
                    filter_lifetime_hours=500.0, platform_cost_rub=2000.0)
        base.update(over)
        return base

    def test_cost_prediction_is_calculated(self):
        params = self._params()
        slices = slice_stl(CUBE_STL, 0.05)
        time_est = estimate_print_time(slices, params, "steel", stl_bytes=CUBE_STL)
        cost = estimate_cost(slices, params, "steel", time_est)
        assert cost.prediction is not None
        assert cost.prediction.source == PredictionSource.CALCULATED
        assert cost.prediction.value == pytest.approx(cost.total_rub)

    def test_cost_prediction_warns_on_uncalibrated_time(self):
        params = self._params()  # без time_correction_by_mat → физика без калибровки
        slices = slice_stl(CUBE_STL, 0.05)
        time_est = estimate_print_time(slices, params, "steel", stl_bytes=CUBE_STL)
        cost = estimate_cost(slices, params, "steel", time_est)
        assert any("некалиброванному" in w for w in cost.prediction.warnings)

    def test_cost_prediction_no_warning_when_time_calibrated(self):
        params = self._params(time_correction_by_mat={"steel": 1.1})
        slices = slice_stl(CUBE_STL, 0.05)
        time_est = estimate_print_time(slices, params, "steel", stl_bytes=CUBE_STL)
        cost = estimate_cost(slices, params, "steel", time_est)
        assert not any("некалиброванному" in w for w in cost.prediction.warnings)


def _group(readiness=90, anomalies=0, burn_slope=0.0, dq=100):
    return {
        "features": {
            "atmosphere_readiness": readiness,
            "process_anomaly_count": anomalies,
            "data_quality_score": dq,
            "duration_min": 300,
            "layers": 150,
        },
        "health": {"burn_drift": {"slope_sec_per_layer": burn_slope}},
        "signal_stats": {"SO1": {"mean": 0.18, "std": 0.02, "group": "oxygen"}},
    }


class TestDefectRiskPrediction:
    def test_heuristic_prediction_is_heuristic_source(self):
        res = predict_defect_risk(_group())
        assert res["prediction"]["source"] == "heuristic"
        assert res["prediction"]["value"] == res["risk"]
        assert res["prediction"]["sample_size"] is None

    def test_model_prediction_is_model_source_with_sample_size(self):
        good = [(_group(readiness=92 + i % 6, anomalies=0, dq=98 + i % 3), 0) for i in range(12)]
        bad = [(_group(readiness=22 + i % 6, anomalies=5, burn_slope=0.7, dq=48 + i % 3), 1)
               for i in range(12)]
        model = train_defect_model(good + bad)
        assert model is not None
        res = predict_defect_risk(_group(readiness=20, anomalies=6, burn_slope=0.9, dq=45), model)
        assert res["prediction"]["source"] == "model"
        assert res["prediction"]["sample_size"] == model["n_train"]
        assert res["prediction"]["value"] == res["risk"]


class TestMaintenancePrediction:
    def _sessions(self, means, signal="SP4"):
        return [{"session_id": f"s{i}", "signal_stats": {signal: {"mean": m, "group": "pressure"}}}
                for i, m in enumerate(means)]

    def test_forecast_prediction_carries_sample_size(self):
        sessions = self._sessions([1, 2, 3, 4, 5])
        out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
        assert len(out) == 1
        pred = out[0]["prediction"]
        assert pred["source"] == "model"
        assert pred["sample_size"] == 5
        assert pred["value"] == out[0]["sessions_to_threshold"]
        assert pred["unit"] == "печатей"

    def test_forecast_at_minimum_sessions_is_flagged(self):
        sessions = self._sessions([1, 2, 3, 4])  # exactly MIN_SESSIONS
        assert len(sessions) == MIN_SESSIONS
        out = forecast_maintenance(sessions, {"SP4": {"alarm_high": 10.0}})
        assert len(out) == 1
        assert any("минимально" in w for w in out[0]["prediction"]["warnings"])
