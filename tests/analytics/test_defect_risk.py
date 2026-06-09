"""Tests for analytics.prediction.defect_risk."""
from analytics.prediction.defect_risk import (
    MIN_LABELS,
    build_feature_row,
    outcome_to_label,
    predict_defect_risk,
    train_defect_model,
)


def _group(readiness=90, anomalies=0, burn_slope=0.0, dq=100, o2_mean=0.18, o2_std=0.02,
           duration=300, layers=150):
    return {
        "features": {
            "atmosphere_readiness": readiness,
            "process_anomaly_count": anomalies,
            "data_quality_score": dq,
            "duration_min": duration,
            "layers": layers,
        },
        "health": {"burn_drift": {"slope_sec_per_layer": burn_slope}},
        "signal_stats": {"SO1": {"mean": o2_mean, "std": o2_std, "group": "oxygen"}},
    }


def test_outcome_label_mapping():
    assert outcome_to_label("accepted") == 0
    assert outcome_to_label("rejected") == 1
    assert outcome_to_label("брак") == 1
    assert outcome_to_label("unknown junk") is None
    assert outcome_to_label(None) is None


def test_heuristic_clean_session_low_risk():
    res = predict_defect_risk(_group(readiness=95, anomalies=0, burn_slope=0.0, dq=100))
    assert res["method"] == "heuristic"
    assert res["risk"] < 0.3
    assert res["grade"] == "low"


def test_heuristic_bad_session_high_risk():
    res = predict_defect_risk(_group(readiness=20, anomalies=6, burn_slope=0.8, dq=40))
    assert res["risk"] > 0.6
    assert res["grade"] == "high"
    assert res["top_factors"]  # explainable
    assert all("factor" in f and "contribution" in f for f in res["top_factors"])


def test_model_not_trained_below_min_labels():
    data = [(_group(), 0) for _ in range(MIN_LABELS - 1)]
    assert train_defect_model(data) is None


def test_model_not_trained_single_class():
    data = [(_group(), 0) for _ in range(MIN_LABELS + 2)]  # all good, one class
    assert train_defect_model(data) is None


def test_model_trains_and_predicts_with_separable_data():
    # Good sessions: high readiness, no anomalies. Defects: low readiness, anomalies.
    good = [(_group(readiness=95, anomalies=0, burn_slope=0.0, dq=100), 0) for _ in range(6)]
    bad = [(_group(readiness=25, anomalies=5, burn_slope=0.7, dq=50), 1) for _ in range(6)]
    model = train_defect_model(good + bad)
    assert model is not None
    assert model["n_train"] == 12
    assert model["n_defects"] == 6

    risk_bad = predict_defect_risk(_group(readiness=20, anomalies=6, burn_slope=0.9, dq=45), model)
    risk_good = predict_defect_risk(_group(readiness=98, anomalies=0, burn_slope=0.0, dq=100), model)
    assert risk_bad["method"] == "model"
    assert risk_bad["risk"] > risk_good["risk"]
    assert risk_bad["top_factors"]


def test_build_feature_row_handles_missing_fields():
    row = build_feature_row({})
    assert row["anomaly_count"] == 0.0
    assert row["readiness"] is None  # absent → None, not crash
