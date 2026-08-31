import numpy as np

from analytics.process_monitoring.advanced import build_advanced_monitoring
from analytics.process_monitoring.change_points import detect_bayesian_change_points
from analytics.process_monitoring.dynamic_pca import analyze_dynamic_pca
from analytics.process_monitoring.hsmm import explicit_duration_viterbi, segment_layer_regimes
from analytics.process_monitoring.neural_models import (
    neural_layer_forecast,
    neural_telemetry_autoencoder,
)
from analytics.process_monitoring.process_mining import discover_process_model
from analytics.process_monitoring.sequence_alignment import (
    compare_similar_layer_sequences,
    dynamic_time_warping,
)
from analytics.process_monitoring.subsequence import analyze_matrix_profile


def test_dynamic_pca_detects_correlated_regime_deviation():
    rng = np.random.default_rng(42)
    base = np.linspace(0, 2, 100) + rng.normal(0, 0.03, 100)
    second = 2 * base + rng.normal(0, 0.03, 100)
    second[75:80] += 3.0

    result = analyze_dynamic_pca({"SO1": base.tolist(), "SO2": second.tolist()})

    assert result["status"] == "ok"
    assert result["mode"] == "shadow"
    assert result["anomaly_count"] > 0
    assert any(75 <= item["index"] <= 82 for item in result["anomalies"])


def test_matrix_profile_finds_injected_discord():
    pattern = np.sin(np.linspace(0, np.pi * 2, 12))
    values = np.tile(pattern, 10)
    values[72:84] = np.linspace(-4, 4, 12)

    result = analyze_matrix_profile(values.tolist(), window=10)

    assert result["status"] == "ok"
    assert 62 <= result["discord"]["start_index"] <= 82
    assert result["motif"]["distance"] < result["discord"]["distance"]


def test_bocpd_ranks_mean_shift_near_true_boundary():
    rng = np.random.default_rng(7)
    values = np.r_[rng.normal(0, 0.15, 60), rng.normal(3, 0.15, 60)]

    result = detect_bayesian_change_points(values.tolist(), expected_run_length=60)

    assert result["status"] == "ok"
    candidates = [item["index"] for item in result["change_points"]]
    assert any(58 <= index <= 63 for index in candidates) or 58 <= result["strongest_index"] <= 63


def test_dtw_aligns_stretched_but_similar_sequences():
    reference = np.sin(np.linspace(0, np.pi * 3, 80))
    stretched = np.sin(np.linspace(0, np.pi * 3, 105))
    unrelated = np.random.default_rng(3).normal(size=105)

    similar = dynamic_time_warping(reference.tolist(), stretched.tolist())
    different = dynamic_time_warping(reference.tolist(), unrelated.tolist())

    assert similar["status"] == "ok"
    assert similar["distance"] < different["distance"]


def test_cross_session_dtw_only_compares_similar_layer_counts():
    sessions = [
        {"session_id": "a", "layer_burn_values": list(np.sin(np.linspace(0, 4, 100)))},
        {"session_id": "different", "layer_burn_values": [1.0] * 40},
        {"session_id": "b", "layer_burn_values": list(np.sin(np.linspace(0, 4, 108)))},
    ]
    comparisons = compare_similar_layer_sequences(sessions)

    assert len(comparisons) == 1
    assert comparisons[0]["reference_session_id"] == "a"
    assert comparisons[0]["candidate_session_id"] == "b"
    assert comparisons[0]["mode"] == "shadow"


def test_hsmm_respects_explicit_durations_and_segments_regimes():
    observations = [0.0] * 8 + [5.0] * 9
    states = [
        {"name": "normal", "mean": 0, "std": 0.3, "min_duration": 4,
         "max_duration": 12, "duration_mean": 8, "duration_std": 2},
        {"name": "slow", "mean": 5, "std": 0.3, "min_duration": 4,
         "max_duration": 12, "duration_mean": 9, "duration_std": 2},
    ]
    decoded = explicit_duration_viterbi(observations, states)

    assert decoded["status"] == "ok"
    assert [(item["state"], item["duration"]) for item in decoded["segments"]] == [
        ("normal", 8), ("slow", 9),
    ]
    assert segment_layer_regimes([10.0] * 12 + [15.0] * 12)["status"] == "ok"


def test_process_mining_exposes_reference_conformance_and_russian_names():
    events = [
        {"event_type": "print_start"},
        {"event_type": "burn_start"},
        {"event_type": "pour_start"},
        {"event_type": "burn_start"},
        {"event_type": "print_complete"},
    ]
    result = discover_process_model(events)

    assert result["status"] == "ok"
    assert result["conformance_score"] == 1.0
    assert result["trace"][1]["name_ru"] == "Лазерное сканирование"


def test_advanced_monitoring_never_authorizes_operator_action():
    telemetry = {
        "oxygen": {"SO1": list(range(50)), "SO2": [value * 2 for value in range(50)]},
        "layer_burn_times": [
            {"layer": index, "duration_sec": 10 + (index % 5) * 0.1}
            for index in range(1, 51)
        ],
    }
    result = build_advanced_monitoring(telemetry, [])

    assert result["mode"] == "shadow"
    assert result["operator_action_allowed"] is False
    assert set(result["algorithms"]) == {
        "dynamic_pca", "matrix_profile", "bayesian_change_points", "hsmm_layer_regimes",
        "neural_autoencoder", "neural_layer_forecast",
    }


def test_neural_autoencoder_is_shadow_and_benchmarked_against_pca():
    rng = np.random.default_rng(11)
    x = np.linspace(-1, 1, 120)
    y = x**2 + rng.normal(0, 0.02, len(x))
    y[100:105] += 2
    result = neural_telemetry_autoencoder({"SO1": x.tolist(), "ST5": y.tolist()})

    assert result["status"] == "ok"
    assert result["operator_action_allowed"] is False
    assert isinstance(result["quality_gate_passed"], bool)
    assert result["pca_validation_mse"] >= 0
    assert result["anomaly_count"] > 0


def test_neural_layer_forecast_has_chronological_baseline_gate():
    values = 10 + np.sin(np.linspace(0, 20, 140))
    result = neural_layer_forecast(values.tolist())

    assert result["status"] == "ok"
    assert result["operator_action_allowed"] is False
    assert isinstance(result["quality_gate_passed"], bool)
    assert len(result["forecast_interval_sec"]) == 2
