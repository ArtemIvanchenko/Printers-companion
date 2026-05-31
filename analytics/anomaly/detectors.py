from typing import Any
from uuid import uuid4

from analytics.baselines.robust import robust_z_scores


def detect_layer_duration_anomalies(layer_features: list[dict[str, Any]], z_threshold: float = 3.5) -> list[dict[str, Any]]:
    durations = [
        float(feature["layer_duration_sec"])
        for feature in layer_features
        if feature.get("layer_duration_sec") is not None
    ]
    scores = robust_z_scores(durations)
    anomalies: list[dict[str, Any]] = []
    duration_index = 0
    for feature in layer_features:
        if feature.get("layer_duration_sec") is None:
            continue
        score = scores[duration_index]
        duration_index += 1
        if abs(score) >= z_threshold:
            anomalies.append(
                {
                    "anomaly_id": f"anomaly_{uuid4().hex}",
                    "anomaly_type": "layer_duration_outlier",
                    "layer": feature["layer"],
                    "severity": "warning",
                    "confidence": min(abs(score) / (z_threshold * 2), 0.95),
                    "features": feature | {"robust_z": score},
                    "evidence": [{"kind": "derived_feature", "layer": feature["layer"]}],
                }
            )
    return anomalies


def detect_state_churn(transitions: list[dict[str, Any]], threshold: int = 20) -> list[dict[str, Any]]:
    if len(transitions) < threshold:
        return []
    return [
        {
            "anomaly_id": f"anomaly_{uuid4().hex}",
            "anomaly_type": "state_churn",
            "severity": "warning",
            "confidence": 0.65,
            "features": {"transition_count": len(transitions), "threshold": threshold},
            "evidence": [{"kind": "state_transition_summary"}],
        }
    ]

