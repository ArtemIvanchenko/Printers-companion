import pytest

from analytics.soft_sensors import compute_soft_sensors


def test_physics_informed_soft_sensors_are_explainable():
    telemetry = {
        "oxygen": {"SO1": [0.5, 0.5, 0.6, 0.5, 0.5], "SO2": [0.4, 0.5, 0.5, 0.4, 0.5]},
        "temperatures": {"ST3": [100, 101, 102, 103, 104], "ST5": [110, 111, 112, 113, 114]},
        "gas_temperature": {"Flow T": [20, 20, 21, 21, 20]},
        "humidity": {"Flow H": [40, 42, 41, 43, 40]},
        "pressure": {"SP4": [1.0, 0.99, 0.98, 0.97, 0.96]},
    }
    result = compute_soft_sensors(
        telemetry,
        {"SP11": {"mean": 1.2}, "SP12": {"mean": 1.0}},
    )
    by_key = {metric["key"]: metric for metric in result["metrics"]}

    assert set(by_key) == {
        "oxygen_sensor_disagreement",
        "thermal_nonuniformity",
        "purge_gas_dew_point",
        "chamber_pressure_drift",
        "filter_pressure_drop_proxy",
    }
    assert by_key["thermal_nonuniformity"]["value"] == 10
    assert by_key["filter_pressure_drop_proxy"]["value"] == pytest.approx(0.2)
    assert by_key["purge_gas_dew_point"]["inputs"] == ["Flow T", "Flow H"]
    assert all(metric["method"] and metric["name_ru"] for metric in result["metrics"])


def test_soft_sensors_skip_missing_channels_instead_of_fabricating_values():
    assert compute_soft_sensors({"oxygen": {"SO1": [0.1] * 10}})["metrics"] == []
