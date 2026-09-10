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
  text dump (JSON-serialisable); explanations use local per-session SHAP
  contributions rather than global training-set importance.

Inputs are the ``group`` payloads already stored per session
(``features`` + ``health`` + ``signal_stats`` + ``data_quality``), so no
re-parsing of raw logs is needed.
"""
from __future__ import annotations

import math
from typing import Any

from analytics.prediction.contract import PredictionResult, PredictionSource

# Minimum labelled sessions (with BOTH classes present) before we trust a model.
#
# Was 8 — with 8 features that is one observation per parameter, where a
# logistic regression separates the data perfectly and reports confident
# 0.0/1.0 risks that carry no information. A model is now additionally
# required to beat MIN_CV_AUC under cross-validation before it is used at all,
# so these floors are the entry ticket, not the whole check.
MIN_LABELS = 20
# With this many labels LightGBM replaces the logistic regression.
MIN_LABELS_GBM = 40
# Number of stratified folds used to estimate out-of-sample quality.
CV_FOLDS = 5
# Below this cross-validated ROC AUC the model is no better than guessing on
# this shop's data — fall back to the transparent heuristic instead of showing
# an operator a number that only looks like a prediction. 0.5 = coin flip.
MIN_CV_AUC = 0.65

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
    risk = round(risk, 4)
    return {
        "risk": risk,
        "grade": _grade(risk),
        "method": "heuristic",
        "top_factors": [t for t in top if t["contribution"] > 0][:4],
        "prediction": PredictionResult(
            value=risk,
            unit="0..1",
            source=PredictionSource.HEURISTIC,
            sample_size=None,
            explanation=(
                "Прозрачная взвешенная эвристика по известным факторам риска — "
                "обученной модели пока нет или она не прошла кросс-валидацию."
            ),
        ).to_dict(),
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


def _cross_val_auc(
    X: list[list[float]], y: list[int], kind: str, usable: list[str],
) -> tuple[float, int] | None:
    """Forward-chaining ROC AUC — an out-of-time quality estimate.

    Input rows are chronological. Every fold trains only on earlier sessions
    and scores a later block, preventing future process state from leaking into
    the past through a shuffled split. Folds without both classes on either
    side cannot define ROC AUC and are skipped.
    """
    try:
        import numpy as np
        from sklearn.metrics import roc_auc_score
        from sklearn.model_selection import TimeSeriesSplit
    except Exception:
        return None

    Xa, ya = np.asarray(X, dtype=float), np.asarray(y)
    n_splits = min(CV_FOLDS, max(2, len(ya) // 4))
    if len(ya) <= n_splits:
        return None

    observed: list[int] = []
    predicted: list[float] = []
    valid_folds = 0
    try:
        for train_idx, test_idx in TimeSeriesSplit(n_splits=n_splits).split(Xa):
            if len(set(ya[train_idx])) < 2 or len(set(ya[test_idx])) < 2:
                continue
            fitted = _fit(Xa[train_idx].tolist(), ya[train_idx].tolist(), kind, usable)
            if fitted is None:
                continue
            scores = [_raw_score(fitted, row) for row in Xa[test_idx].tolist()]
            if any(s is None for s in scores):
                continue
            observed.extend(int(value) for value in ya[test_idx])
            predicted.extend(float(score) for score in scores if score is not None)
            valid_folds += 1
        if valid_folds < 2 or len(set(observed)) < 2:
            return None
        return float(roc_auc_score(observed, predicted)), valid_folds
    except Exception:
        return None


def _fit(X: list[list[float]], y: list[int], kind: str, usable: list[str]) -> dict[str, Any] | None:
    """Fit one model of the requested kind. No validation, no gating."""
    if kind == "lightgbm":
        return _train_lightgbm(X, y, usable)
    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler

        Xa = np.asarray(X, dtype=float)
        scaler = StandardScaler().fit(Xa)
        clf = LogisticRegression(max_iter=1000).fit(scaler.transform(Xa), y)
    except Exception:
        return None

    return {
        "type": "logreg",
        "features": usable,
        "mean": scaler.mean_.tolist(),
        "scale": [s if s else 1.0 for s in scaler.scale_.tolist()],
        "coef": clf.coef_[0].tolist(),
        "intercept": float(clf.intercept_[0]),
    }


def _raw_score(model: dict[str, Any], values: list[float]) -> float | None:
    """Probability of the positive class for one already-ordered feature vector."""
    row = dict(zip(model["features"], values))
    result = _lightgbm_risk(row, model) if model.get("type") == "lightgbm" else _model_risk(row, model)
    return None if result is None else result["risk"]


def train_defect_model(
    groups_with_labels: list[tuple[dict[str, Any], int]],
) -> dict[str, Any] | None:
    """Fit a defect model on labelled sessions, or None to use the heuristic.

    ≥ MIN_LABELS_GBM labels → LightGBM (non-linear, tabular SOTA);
    ≥ MIN_LABELS → standardised logistic regression.

    A fitted model is only returned when its **cross-validated** ROC AUC clears
    MIN_CV_AUC. Without that gate a model fitted on a handful of rows separates
    them perfectly and then reports its own training labels back as confident
    "predictions" — indistinguishable, on screen, from a model that works.
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
    minority = min(sum(y), len(y) - sum(y))

    kinds = ["logreg"]
    if len(y) >= MIN_LABELS_GBM and minority >= 10:
        kinds.insert(0, "lightgbm")

    for kind in kinds:
        cv_result = _cross_val_auc(X, y, kind, usable)
        if cv_result is None:
            continue
        auc, valid_folds = cv_result
        if auc < MIN_CV_AUC:
            continue
        model = _fit(X, y, kind, usable)
        if model is None:
            continue
        model.update({
            "n_train": len(y),
            "n_defects": int(sum(y)),
            "cv_auc": round(auc, 3),
            "cv_folds": valid_folds,
        })
        return model
    return None


def _model_prediction(risk: float, model: dict[str, Any], method: str) -> dict[str, Any]:
    """Shared PredictionResult wrapper for both learned-model risk paths."""
    n_train = model.get("n_train")
    cv_auc = model.get("cv_auc")
    return PredictionResult(
        value=risk,
        unit="0..1",
        source=PredictionSource.MODEL,
        sample_size=n_train if isinstance(n_train, int) else None,
        explanation=(
            f"Обученная модель ({method}), прошедшая кросс-валидацию "
            f"(AUC={cv_auc}) на {n_train} размеченных сессиях."
            if n_train is not None else
            f"Обученная модель ({method}), прошедшая кросс-валидацию."
        ),
    ).to_dict()


def _lightgbm_risk(row: dict[str, float | None], model: dict[str, Any]) -> dict[str, Any] | None:
    feats = model["features"]
    if any(row.get(f) is None for f in feats):
        return None
    try:
        import lightgbm as lgb
        import numpy as np

        booster = lgb.Booster(model_str=model["model_str"])
        matrix = np.asarray([[float(row[f]) for f in feats]])
        risk = float(booster.predict(matrix)[0])
        local = booster.predict(matrix, pred_contrib=True)[0][:-1]
    except Exception:
        return None
    top = sorted(
        (
            {
                "factor": _LABEL.get(feature, feature),
                "contribution": round(float(contribution), 4),
                "direction": "raises_risk" if contribution > 0 else "lowers_risk",
            }
            for feature, contribution in zip(feats, local)
        ),
        key=lambda item: -abs(item["contribution"]),
    )
    risk = round(risk, 4)
    return {
        "risk": risk,
        "grade": _grade(risk),
        "method": "lightgbm",
        "top_factors": [item for item in top if item["contribution"] != 0][:4],
        "model_info": _model_info(model),
        "prediction": _model_prediction(risk, model, "LightGBM"),
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
    risk = round(1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, z)))), 4)
    top = sorted(
        ({"factor": _LABEL.get(f, f), "contribution": round(c, 4)} for f, c in contributions),
        key=lambda d: -abs(d["contribution"]),
    )
    return {
        "risk": risk,
        "grade": _grade(risk),
        "method": "model",
        "top_factors": top[:4],
        "model_info": _model_info(model),
        "prediction": _model_prediction(risk, model, "логистическая регрессия"),
    }


def _model_info(model: dict[str, Any]) -> dict[str, Any]:
    """Provenance shown next to a learned risk score.

    ``cv_auc`` is the out-of-sample quality gate the model had to clear; it is
    surfaced so an operator can see how much the number is worth rather than
    only that "a model exists".
    """
    return {
        "n_train": model.get("n_train"),
        "n_defects": model.get("n_defects"),
        "cv_auc": model.get("cv_auc"),
        "cv_folds": model.get("cv_folds"),
        "model_version_id": model.get("model_version_id"),
        "registry_status": model.get("registry_status"),
        "training_fingerprint": model.get("training_fingerprint"),
        "app_version": model.get("app_version"),
        "analysis_version": model.get("analysis_version"),
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
