"""Preview projection keeps the calculator's time scope and physical layers."""
import asyncio
from types import SimpleNamespace

import pytest

from analytics.prediction.contract import PredictionResult, PredictionSource
from analytics.prediction.print_time import PrintTimeEstimate
from analytics.prediction.stl_slicer import SliceResult
from api.routes.uploads import _geometry_prediction


@pytest.mark.parametrize("scope", ["burn_plus_pour", "machine_cycle"])
def test_preview_preserves_nullable_cycle_and_does_not_attach_phase_interval(scope, monkeypatch):
    from storage.repositories.prints_repo import PrintsRepository
    from analytics.prediction import print_time, stl_slicer, cost_estimator, accuracy

    params = {"hatch_speed_mm_s": 100, "contour_speed_mm_s": 100,
              "hatch_distance_mm": .1, "layer_thickness_mm": .1,
              "laser_count": 1, "recoat_time_ms": 1000}
    monkeypatch.setattr(PrintsRepository, "get_machine_params", lambda self: params)
    monkeypatch.setattr(PrintsRepository, "get_active_preset_for_material", lambda *a: None)
    monkeypatch.setattr(PrintsRepository, "last_powder_cost", lambda self: None)
    slices = SliceResult(volume_mm3=1, height_mm=.2, layer_count=2, layer_thickness_mm=.1)
    estimate = PrintTimeEstimate(scan_hours=1, recoat_hours=1, print_hours=2,
        total_days=None if scope == "burn_plus_pour" else .125, method="cohatch",
        breakdown={"layer_count": 12, "prediction_time_scope": scope,
                   "machine_cycle_hours": None if scope == "burn_plus_pour" else 3},
        prediction=PredictionResult(value=2 if scope == "burn_plus_pour" else 3,
                                    unit="ч", source=PredictionSource.CALCULATED))
    monkeypatch.setattr(stl_slicer, "slice_stl", lambda *a: slices)
    monkeypatch.setattr(print_time, "estimate_print_time", lambda *a, **kw: estimate)
    monkeypatch.setattr(cost_estimator, "estimate_cost", lambda *a, **kw: SimpleNamespace(
        total_rub=100, breakdown={}, powder_kg=None, prediction=None, warnings=[]))
    calls = []
    def phase_interval(*args):
        calls.append(args)
        return (1.5, 2.5)
    monkeypatch.setattr(accuracy, "calibration_interval_hours", phase_interval)
    result = asyncio.run(_geometry_prediction(b"fixture.stl", "steel"))
    assert result["available"] is True, result
    assert result["layer_count"] == 12
    assert result["total_days"] == (None if scope == "burn_plus_pour" else .12)
    assert bool(calls) == (scope == "burn_plus_pour")
    assert result["prediction"]["interval"] == ([1.5, 2.5] if scope == "burn_plus_pour" else None)
