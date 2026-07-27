"""Tests for analytics.prediction.defect_risk."""
from analytics.prediction.defect_risk import (
    MIN_CV_AUC,
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
    # Slight jitter per row so the folds are not made of identical duplicates.
    good = [(_group(readiness=92 + i % 6, anomalies=0, burn_slope=0.0, dq=98 + i % 3), 0)
            for i in range(12)]
    bad = [(_group(readiness=22 + i % 6, anomalies=5, burn_slope=0.7, dq=48 + i % 3), 1)
           for i in range(12)]
    model = train_defect_model(good + bad)
    assert model is not None
    assert model["n_train"] == 24
    assert model["n_defects"] == 12
    # Quality is measured out-of-sample and surfaced, not assumed.
    assert model["cv_auc"] >= MIN_CV_AUC

    risk_bad = predict_defect_risk(_group(readiness=20, anomalies=6, burn_slope=0.9, dq=45), model)
    risk_good = predict_defect_risk(_group(readiness=98, anomalies=0, burn_slope=0.0, dq=100), model)
    assert risk_bad["method"] == "model"
    assert risk_bad["risk"] > risk_good["risk"]
    assert risk_bad["top_factors"]
    assert risk_bad["model_info"]["cv_auc"] == model["cv_auc"]


def test_model_is_rejected_when_labels_carry_no_signal():
    """Regression: a model used to be accepted on any label set that cleared a
    count threshold. Fitted on noise it separates its own training rows and
    reports confident risks that mean nothing — the operator cannot tell the
    difference on screen. Cross-validation must send this back to the heuristic.
    """
    import random

    rng = random.Random(0)
    data = [
        (_group(readiness=rng.uniform(20, 100), anomalies=rng.randint(0, 6),
                burn_slope=rng.uniform(0, 0.9), dq=rng.uniform(40, 100)),
         rng.randint(0, 1))
        for _ in range(40)
    ]
    assert train_defect_model(data) is None

    # …and scoring without a model degrades to the transparent heuristic.
    assert predict_defect_risk(_group(), None)["method"] == "heuristic"


def test_labels_below_new_minimum_are_not_enough():
    # 12 labelled sessions across 8 features is roughly one row per parameter.
    good = [(_group(readiness=95, anomalies=0), 0) for _ in range(6)]
    bad = [(_group(readiness=25, anomalies=5), 1) for _ in range(6)]
    assert train_defect_model(good + bad) is None


def test_build_feature_row_handles_missing_fields():
    row = build_feature_row({})
    assert row["anomaly_count"] == 0.0
    assert row["readiness"] is None  # absent → None, not crash
