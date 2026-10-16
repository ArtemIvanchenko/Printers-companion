import pytest

from analytics.soft_sensors import compute_soft_sensors


@pytest.mark.parametrize("inactive", [[None] * 6, [20, 21, None, None, None, None]])
def test_thermal_metric_names_only_channels_actually_used(inactive):
    # Input order differs from the conventional display order; both retained
    # temperature channels have enough aligned measurements.
    temperatures = {"ST5": [110] * 6, "ST4": inactive, "ST3": [100] * 6}
    result = compute_soft_sensors({"temperatures": temperatures})
    [metric] = result["metrics"]
    assert metric["key"] == "thermal_nonuniformity"
    assert metric["inputs"] == ["ST3", "ST5"]
    assert metric["value"] == metric["p95"] == 10
    assert metric["status"] == "diagnostic"
    # Removing a channel already rejected from the calculation changes no
    # output: it cannot be credited as evidence while absent from the math.
    temperatures.pop("ST4")
    assert compute_soft_sensors({"temperatures": temperatures}) == result


def test_thermal_metric_keeps_a_third_channel_when_it_has_measurements():
    result = compute_soft_sensors({"temperatures": {
        "ST5": [110] * 6, "ST4": [20] * 6, "ST3": [100] * 6,
    }})
    [metric] = result["metrics"]
    assert metric["inputs"] == ["ST3", "ST4", "ST5"]
    assert metric["value"] == metric["p95"] == 90
