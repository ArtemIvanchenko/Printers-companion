import math

from parsers.common.numeric_quality import neural_reconstruction_profile, robust_numeric_profile


def test_flat_channel_changes_are_not_all_labeled_spikes() -> None:
    rows = [{"signal": 0.0} for _ in range(90)] + [{"signal": 1.0} for _ in range(10)]

    result = robust_numeric_profile(rows)

    assert result["columns"]["signal"]["robust_spike_count"] == 0


def test_neural_reconstruction_flags_injected_multivariate_corruption() -> None:
    rows = []
    for index in range(700):
        base = math.sin(index / 25)
        rows.append({"a": base, "b": 2 * base + 0.01 * math.cos(index)})
    rows[660] = {"a": 20.0, "b": -20.0}

    result = neural_reconstruction_profile(rows, startup_rows=0, max_rows=700)

    assert result["status"] == "ok"
    assert result["anomaly_count"] >= 1
    assert 660 in result["top_sample_indices"]
