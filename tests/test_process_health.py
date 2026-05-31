from analytics.process_health import (
    analyze_layer_burn_drift,
    atmosphere_readiness_score,
    build_process_health,
    detect_process_anomalies,
)


def test_oxygen_spike_is_flagged():
    telemetry = {"oxygen": {"SO1": [9, 9, 9, 9, 9, 9, 9, 30]}}
    anomalies = detect_process_anomalies(telemetry)
    assert any(a["signal"] == "SO1" and a["kind"] == "spike" for a in anomalies)
    assert anomalies[0]["semantic"] == "кислород"
    assert anomalies[0]["severity"] == "high"


def test_stable_series_has_no_anomalies():
    telemetry = {"oxygen": {"SO1": [9.0] * 10}, "humidity": {"Flow H": [0.1] * 10}}
    assert detect_process_anomalies(telemetry) == []


def test_burn_drift_detects_rising_trend():
    burns = [{"layer": i, "duration_sec": 10 + i} for i in range(1, 12)]
    result = analyze_layer_burn_drift(burns)
    assert result["trend"] == "rising"
    assert result["slope_sec_per_layer"] > 0
    assert result["mean_sec"] is not None


def test_burn_drift_flags_outlier_layer():
    burns = [{"layer": i, "duration_sec": 10} for i in range(1, 12)]
    burns[5]["duration_sec"] = 200  # one wild layer
    result = analyze_layer_burn_drift(burns)
    assert any(o["layer"] == burns[5]["layer"] for o in result["outlier_layers"])


def test_burn_drift_insufficient_data():
    assert analyze_layer_burn_drift([{"layer": 1, "duration_sec": 5}])["trend"] == "insufficient_data"


def test_readiness_high_for_stable_atmosphere():
    telemetry = {
        "oxygen": {"SO1": [9.0] * 10},
        "pressure": {"SP4": [1.0] * 10},
        "humidity": {"Flow H": [0.1] * 10},
    }
    r = atmosphere_readiness_score(telemetry)
    assert r["score"] >= 90
    assert r["grade"] == "good"


def test_readiness_lower_for_unstable_atmosphere():
    telemetry = {"oxygen": {"SO1": [1, 50, 2, 80, 3, 90, 1, 70, 2, 60]}}
    r = atmosphere_readiness_score(telemetry)
    assert r["score"] < 75


def test_build_process_health_bundle_and_empty():
    empty = build_process_health({})
    assert empty["anomalies"] == []
    assert empty["readiness"]["score"] is None

    full = build_process_health({
        "oxygen": {"SO1": [9.0] * 10},
        "pressure": {"SP4": [1.0] * 10},
        "layer_burn_times": [{"layer": i, "duration_sec": 10 + i} for i in range(1, 12)],
    })
    assert "anomalies" in full and "burn_drift" in full and "readiness" in full
    assert full["burn_drift"]["trend"] == "rising"
