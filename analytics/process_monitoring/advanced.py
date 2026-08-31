"""One guarded entry point for all shadow process-monitoring algorithms."""

from __future__ import annotations

from typing import Any

from analytics.process_monitoring.change_points import detect_bayesian_change_points
from analytics.process_monitoring.dynamic_pca import analyze_dynamic_pca
from analytics.process_monitoring.hsmm import segment_layer_regimes
from analytics.process_monitoring.neural_models import (
    neural_layer_forecast,
    neural_telemetry_autoencoder,
)
from analytics.process_monitoring.process_mining import discover_process_model
from analytics.process_monitoring.subsequence import analyze_matrix_profile


def _telemetry_series(telemetry: dict[str, Any]) -> dict[str, list[Any]]:
    wanted = ("oxygen", "temperatures", "pressure", "humidity")
    result: dict[str, list[Any]] = {}
    for group in wanted:
        for signal, values in (telemetry.get(group) or {}).items():
            if isinstance(values, list):
                result[signal] = values
    return result


def build_advanced_monitoring(telemetry: dict[str, Any], events: list[Any]) -> dict[str, Any]:
    """Compute diagnostics without allowing an experimental model to raise alarms."""
    burn_points = telemetry.get("layer_burn_times") or []
    burn_values = [point.get("duration_sec") for point in burn_points if isinstance(point, dict)]
    telemetry_series = _telemetry_series(telemetry)
    algorithms = {
        "dynamic_pca": analyze_dynamic_pca(telemetry_series),
        "matrix_profile": analyze_matrix_profile(burn_values),
        "bayesian_change_points": detect_bayesian_change_points(burn_values),
        "hsmm_layer_regimes": segment_layer_regimes(burn_values),
        "neural_autoencoder": neural_telemetry_autoencoder(telemetry_series),
        "neural_layer_forecast": neural_layer_forecast(burn_values),
    }
    process_model = discover_process_model(events)
    successful = sum(result.get("status") == "ok" for result in algorithms.values())
    if process_model.get("status") == "ok":
        successful += 1
    return {
        "status": (
            "ok" if successful == len(algorithms) + 1
            else "partial" if successful else "insufficient_data"
        ),
        "mode": "shadow",
        "operator_action_allowed": False,
        "successful_algorithms": successful,
        "algorithms": algorithms,
        "process_model": process_model,
        "method_version": "advanced-monitoring-0.1.0",
        "validation_gate_ru": (
            "Не использовать для остановки печати до ретроспективной проверки "
            "на размеченных успешных и дефектных деталях"
        ),
    }


__all__ = ["build_advanced_monitoring"]
