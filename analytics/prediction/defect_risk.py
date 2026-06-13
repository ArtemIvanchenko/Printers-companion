"""Explainable defect-risk scoring for a print session.

Two modes, chosen automatically:

* **Heuristic** (default / cold start): a transparent weighted blend of known
  risk drivers — poor atmosphere readiness, oxygen anomalies, rising burn-time
  drift, low data-quality. Works from the very first session, no training data.
* **Learned**: once enough operator-labelled outcomes exist (good vs defect),
  fit a standardised logistic regression and use its probability. The model is
  stored as plain numbers (means/scales/coefficients) — JSON-serialisable and
  fully explainable via per-feature contributions, not a black box.
* **Gradient boosting**: with a larger labelled history (≥ MIN_LABELS_GBM)
  a LightGBM classifier replaces the logistic regression — it captures
  non-linear interactions tabular data is famous for. Stored as the booster's
  text dump (JSON-serialisable); explanations via per-feature importance.

Inputs are the ``group`` payloads already stored per session
(``features`` + ``health`` + ``signal_stats`` + ``data_quality``), so no
re-parsing of raw logs is needed.
"""
from __future__ import annotations

import math
from typing import Any

# Minimum labelled sessions (with BOTH classes present) before we trust a model.
MIN_LABELS = 8
# With this many labels LightGBM replaces the logistic regression.
MIN_LABELS_GBM = 20

# Raw features pulled from a session group payload. Each entry:
#   key, extractor(group) -> float|None
# The heuristic and the model both consume these; the model learns signs/weights,
# the heuristic applies the documented directions below.
_FEATURES: list[str] = [
    "readiness", "anomaly_count", "burn_slope",
    "data_quality", "o2_mean", "o2_std", "duration_min", "layers",
]

# Outcome result strings → label. 1 = defect (positive class), 0 = good.
_DEFECT_RESULTS = {"rejected", "defect", "fail", "failed", "scrap", "scrapped", "брак"}
_GOOD_RESULTS = {"accepted", "good", "ok", "pass", "passed", "годная", "годен"}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def build_feature_row(group: dict[str, Any]) -> dict[str, float | None]:
    """Extract the raw feature vector from a stored session ``group`` payload."""
    features = group.get("features", {}) or {}
    health = group.get("health", {}) or {}
    signal_stats = group.get("signal_stats", {}) or {}
    burn = (health.get("burn_drift") or {})
    o2 = signal_stats.get("SO1") or signal_stats.get("SO2") or {}

    return {
        "readiness":     _num(features.get("atmosphere_readiness")),
        "anomaly_count": _num(features.get("process_anomaly_count")) or 0.0,
        "burn_slope":    _num(burn.get("slope_sec_per_layer")) or 0.0,
        "data_quality":  _num(features.get("data_quality_score")),
        "o2_mean":       _num(o2.get("mean")),
        "o2_std":        _num(o2.get("std")),
        "duration_min":  _num(features.get("duration_min")) or 0.0,
        "layers":        _num(features.get("layers")) or 0.0,
    }


def outcome_to_label(result: str | None) -> int | None:
    """Map a QualityOutcome result string to 1 (defect) / 0 (good) / None."""
    if not result:
        return None
    r = result.strip().lower()
    if r in _DEFECT_RESULTS:
        return 1
    if r in _GOOD_RESULTS:
        return 0
    return None


# ── Heuristic ────────────────────────────────────────────────────────────────

def _heuristic_risk(row: dict[str, float | None]) -> dict[str, Any]:
    """Transparent weighted blend of risk drivers, each normalised to 0..1."""
    contributions: list[tuple[str, float, float]] = []  # (name, risk_0_1, weight)

    readiness = row.get("readiness")
    if readiness is not None:
        contributions.append(("Низкая готовность атмосферы", max(0.0, (100 - readiness) / 100), 0.35))

    dq = row.get("data_quality")
    if dq is not None:
        contributions.append(("Низкое качество данных", max(0.0, (100 - dq) / 100), 0.15))

    anomalies = row.get("anomaly_count") or 0.0
    contributions.append(("Аномалии процесса", min(1.0, anomalies / 5.0), 0.25))

    burn_slope = row.get("burn_slope") or 0.0
    # Rising burn time (>0) is the degradation direction; scale ~0.5 s/layer → 1.0.
    contributions.append(("Рост времени прожига", min(1.0, max(0.0, burn_slope) / 0.5), 0.25))

    total_w = sum(w for _, _, w in contributions) or 1.0
    risk = sum(r * w for _, r, w in contributions) / total_w
    top = sorted(
        ({"factor": n, "contribution": round(r * w / total_w, 4)} for n, r, w in contributions),
        key=lambda d: -d["contribution"],
    )
    return {
        "risk": round(risk, 4),
        "grade": _grade(risk),
        "method": "heuristic",
        "top_factors": [t for t in top if t["contribution"] > 0][:4],
    }


def _grade(risk: float) -> str:
    return "high" if risk >= 0.6 else "medium" if risk >= 0.3 else "low"


# ── Learned model (standardised logistic regression, stored as plain numbers) ──

def _train_lightgbm(X: list[list[float]], y: list[int], usable: list[str]) -> dict[str, Any] | None:
    try:
        import lightgbm as lgb
        import numpy as np

        booster = lgb.train(
            {
                "objective": "binary",
                "metric": "binary_logloss",
                "verbosity": -1,
                # Small-data guards: shallow trees, low leaf requirements
                "num_leaves": 7,
                "max_depth": 3,
                "min_data_in_leaf": 3,
                "learning_rate": 0.1,
            },
            lgb.Dataset(np.asarray(X, dtype=float), label=np.asarray(y), feature_name=usable),
            num_boost_round=60,
        )
    except Exception:
        return None
    importance = booster.feature_importance(importance_type="gain")
    return {
        "type": "lightgbm",
        "features": usable,
        "model_str": booster.model_to_string(),
        "importance": [float(v) for v in importance],
        "n_train": len(y),
        "n_defects": int(sum(y)),
    }


def train_defect_model(
    groups_with_labels: list[tuple[dict[str, Any], int]],
) -> dict[str, Any] | None:
    """Fit a defect model on labelled sessions.

    ≥ MIN_LABELS_GBM labels → LightGBM (non-linear, tabular SOTA);
    ≥ MIN_LABELS → standardised logistic regression;
    otherwise None — callers fall back to the heuristic.
    """
    labels = [lbl for _, lbl in groups_with_labels]
    if len(labels) < MIN_LABELS or len(set(labels)) < 2:
        return None

    # Use only features present (non-None) in ALL labelled rows, to avoid imputing.
    rows = [build_feature_row(g) for g, _ in groups_with_labels]
    usable = [f for f in _FEATURES if all(r.get(f) is not None for r in rows)]
    if not usable:
        return None

    X = [[float(r[f]) for f in usable] for r in rows]
    y = labels

    if len(y) >= MIN_LABELS_GBM and min(sum(y), len(y) - sum(y)) >= 5:
        model = _train_lightgbm(X, y, usable)
        if model is not None:
            return model

    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler

        Xa = np.asarray(X, dtype=float)
        scaler = StandardScaler().fit(Xa)
        Xs = scaler.transform(Xa)
        clf = LogisticRegression(max_iter=1000).fit(Xs, y)
    except Exception:
        return None

    return {
        "type": "logreg",
        "features": usable,
        "mean": scaler.mean_.tolist(),
        "scale": [s if s else 1.0 for s in scaler.scale_.tolist()],
        "coef": clf.coef_[0].tolist(),
        "intercept": float(clf.intercept_[0]),
        "n_train": len(y),
        "n_defects": int(sum(y)),
    }


def _lightgbm_risk(row: dict[str, float | None], model: dict[str, Any]) -> dict[str, Any] | None:
    feats = model["features"]
    if any(row.get(f) is None for f in feats):
        return None
    try:
        import lightgbm as lgb
        import numpy as np

        booster = lgb.Booster(model_str=model["model_str"])
        risk = float(booster.predict(np.asarray([[float(row[f]) for f in feats]]))[0])
    except Exception:
        return None
    total_gain = sum(model.get("importance") or []) or 1.0
    top = sorted(
        (
            {"factor": _LABEL.get(f, f), "contribution": round(g / total_gain, 4)}
            for f, g in zip(feats, model.get("importance") or [])
        ),
        key=lambda d: -d["contribution"],
    )
    return {
        "risk": round(risk, 4),
        "grade": _grade(risk),
        "method": "lightgbm",
        "top_factors": [t for t in top if t["contribution"] > 0][:4],
        "model_info": {"n_train": model.get("n_train"), "n_defects": model.get("n_defects")},
    }


def _model_risk(row: dict[str, float | None], model: dict[str, Any]) -> dict[str, Any] | None:
    if model.get("type") == "lightgbm":
        return _lightgbm_risk(row, model)
    feats = model["features"]
    # All model features must be present in this row; otherwise can't score.
    if any(row.get(f) is None for f in feats):
        return None
    contributions = []
    z = model["intercept"]
    for f, mean, scale, coef in zip(feats, model["mean"], model["scale"], model["coef"]):
        std_x = (float(row[f]) - mean) / (scale or 1.0)
        contrib = coef * std_x
        z += contrib
        contributions.append((f, contrib))
    risk = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z))))
    top = sorted(
        ({"factor": _LABEL.get(f, f), "contribution": round(c, 4)} for f, c in contributions),
        key=lambda d: -abs(d["contribution"]),
    )
    return {
        "risk": round(risk, 4),
        "grade": _grade(risk),
        "method": "model",
        "top_factors": top[:4],
        "model_info": {"n_train": model.get("n_train"), "n_defects": model.get("n_defects")},
    }


_LABEL = {
    "readiness": "Готовность атмосферы", "anomaly_count": "Аномалии процесса",
    "burn_slope": "Дрейф времени прожига", "data_quality": "Качество данных",
    "o2_mean": "Средний O₂", "o2_std": "Нестабильность O₂",
    "duration_min": "Длительность", "layers": "Число слоёв",
}


def predict_defect_risk(
    group: dict[str, Any],
    model: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return ``{risk: 0..1, grade, method, top_factors[...]}`` for one session.

    Uses the learned ``model`` when supplied and applicable, else the heuristic.
    """
    row = build_feature_row(group)
    if model:
        result = _model_risk(row, model)
        if result is not None:
            return result
    return _heuristic_risk(row)


__all__ = [
    "build_feature_row", "outcome_to_label", "train_defect_model",
    "predict_defect_risk", "MIN_LABELS", "MIN_LABELS_GBM",
]
