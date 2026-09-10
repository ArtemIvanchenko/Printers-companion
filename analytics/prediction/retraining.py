"""Guarded champion/challenger lifecycle for the defect-risk model.

Numerical work is performed by the operator-PC worker.  The shared database
only stores a compact dataset snapshot and immutable model metadata.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from analytics.prediction.accuracy import PRINT_CLASSIFICATIONS, session_classification
from analytics.prediction.defect_risk import (
    MIN_CV_AUC,
    build_feature_row,
    outcome_to_label,
    predict_defect_risk,
    train_defect_model,
)
from core.versioning.constants import ANALYSIS_VERSION, APP_VERSION
from core.versioning.provenance import git_sha, stable_hash
from domain.models.quality import QualityOutcome
from domain.models.sessions import BuildSession
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.model_registry import ModelRegistryRepository

MODEL_NAME = "defect_risk"
JOB_TYPE = "defect_model_retrain"

# A candidate first proves itself on outcomes that did not exist at training
# time. Eight is deliberately a floor, not a claim of statistical finality.
MIN_SHADOW_LABELS = 8
MIN_SHADOW_CLASS = 3
MIN_BRIER_IMPROVEMENT = 0.01
MIN_AUC_IMPROVEMENT = 0.01


def _latest_labels(db: Session) -> dict[str, int]:
    labels: dict[str, int] = {}
    rows = db.scalars(
        select(QualityOutcome).order_by(QualityOutcome.timestamp, QualityOutcome.outcome_id)
    ).all()
    for row in rows:
        if not row.session_id or not row.is_final:
            continue
        label = outcome_to_label(row.result)
        if label is not None:
            labels[row.session_id] = label
    return labels


def _labelled_sessions(db: Session) -> list[dict[str, Any]]:
    labels = _latest_labels(db)
    rows = db.scalars(select(BuildSession).order_by(BuildSession.start_ts, BuildSession.session_id)).all()
    result: list[dict[str, Any]] = []
    for row in rows:
        if row.session_id not in labels or session_classification(row) not in PRINT_CLASSIFICATIONS:
            continue
        group = ((row.context or {}).get("runtime_payload", {}) or {}).get("group", {}) or {}
        if not group:
            continue
        result.append({
            "session_id": row.session_id,
            "start_ts": row.start_ts.isoformat() if row.start_ts else None,
            "group": group,
            "label": labels[row.session_id],
        })
    return result


def training_fingerprint(rows: list[dict[str, Any]]) -> str:
    return stable_hash([
        {
            "session_id": row["session_id"],
            "start_ts": row.get("start_ts"),
            "label": row["label"],
            "features": build_feature_row(row["group"]),
        }
        for row in rows
    ])


def feature_schema_hash(model: dict[str, Any]) -> str:
    return stable_hash({"features": model.get("features", []), "schema": "defect-risk-v1"})


def enqueue_retraining(
    db: Session,
    *,
    session_id: str,
    outcome_id: str,
    result: str,
    timestamp: str,
) -> dict[str, Any] | None:
    """Enqueue retraining on the PC that owns the labelled session."""
    if outcome_to_label(result) is None:
        return None
    session = db.get(BuildSession, session_id)
    if session is None:
        return None
    event_hash = stable_hash({
        "session_id": session_id,
        "outcome_id": outcome_id,
        "result": result,
        "timestamp": timestamp,
    })[:24]
    return JobsRepository(db).enqueue(
        job_type=JOB_TYPE,
        owner_node_id=session.origin_compute_node_id,
        entity_type="ml_model",
        entity_id=MODEL_NAME,
        idempotency_key=f"{JOB_TYPE}:{session.origin_compute_node_id}:{event_hash}",
        payload={
            "trigger_session_id": session_id,
            "trigger_outcome_id": outcome_id,
            "owner_node_id": session.origin_compute_node_id,
        },
        max_attempts=3,
    )


def enqueue_retraining_for_session(db: Session, session_id: str) -> dict[str, Any] | None:
    """Enqueue from the latest strict label after a card/session link appears."""
    outcome = db.scalar(
        select(QualityOutcome)
        .where(
            QualityOutcome.session_id == session_id,
            QualityOutcome.is_final.is_(True),
        )
        .order_by(QualityOutcome.timestamp.desc(), QualityOutcome.outcome_id.desc())
        .limit(1)
    )
    if outcome is None:
        return None
    timestamp = outcome.timestamp.isoformat() if outcome.timestamp else ""
    return enqueue_retraining(
        db,
        session_id=session_id,
        outcome_id=outcome.outcome_id,
        result=outcome.result,
        timestamp=timestamp,
    )


def active_model(db: Session) -> dict[str, Any] | None:
    """Return the approved artifact, enriched with visible provenance."""
    row = ModelRegistryRepository(db).get_active(MODEL_NAME, include_artifact=True)
    if row is None:
        return None
    artifact = dict(row.get("artifact") or {})
    artifact.update({
        "model_version_id": row["model_version_id"],
        "registry_status": row["status"],
        "app_version": row["app_version"],
        "analysis_version": row["analysis_version"],
        "training_fingerprint": row["training_fingerprint"],
        "training_session_ids": list(row.get("training_session_ids") or []),
    })
    return artifact


def prepare_retraining(db: Session) -> dict[str, Any]:
    registry = ModelRegistryRepository(db)
    return {
        "rows": _labelled_sessions(db),
        "shadow": registry.get_shadow(MODEL_NAME, include_artifact=True),
        "active": registry.get_active(MODEL_NAME, include_artifact=True),
    }


def _binary_metrics(observed: list[int], predicted: list[float]) -> dict[str, Any]:
    from sklearn.metrics import brier_score_loss, roc_auc_score

    result: dict[str, Any] = {
        "sample_size": len(observed),
        "positive_count": int(sum(observed)),
        "negative_count": int(len(observed) - sum(observed)),
        "brier": round(float(brier_score_loss(observed, predicted)), 5),
    }
    result["roc_auc"] = (
        round(float(roc_auc_score(observed, predicted)), 5)
        if len(set(observed)) == 2 else None
    )
    return result


def _shadow_evaluation(
    rows: list[dict[str, Any]],
    shadow: dict[str, Any],
    active: dict[str, Any] | None,
) -> dict[str, Any]:
    trained_ids = set(shadow.get("training_session_ids") or [])
    future = [row for row in rows if row["session_id"] not in trained_ids]
    candidate = dict(shadow.get("artifact") or {})
    comparator = dict((active or {}).get("artifact") or {}) if active else None

    observed: list[int] = []
    candidate_scores: list[float] = []
    comparator_scores: list[float] = []
    evaluated_ids: list[str] = []
    for row in future:
        candidate_result = predict_defect_risk(row["group"], candidate)
        if candidate_result.get("method") == "heuristic":
            # Missing a feature required by the fitted candidate.
            continue
        baseline_result = predict_defect_risk(row["group"], comparator)
        observed.append(int(row["label"]))
        candidate_scores.append(float(candidate_result["risk"]))
        comparator_scores.append(float(baseline_result["risk"]))
        evaluated_ids.append(str(row["session_id"]))

    candidate_metrics = _binary_metrics(observed, candidate_scores) if observed else {
        "sample_size": 0, "positive_count": 0, "negative_count": 0,
        "brier": None, "roc_auc": None,
    }
    comparator_metrics = _binary_metrics(observed, comparator_scores) if observed else {
        "sample_size": 0, "positive_count": 0, "negative_count": 0,
        "brier": None, "roc_auc": None,
    }
    metrics = {
        "candidate": candidate_metrics,
        "comparator": comparator_metrics,
        "comparator_type": "active_model" if active else "transparent_heuristic",
        "evaluated_session_ids": evaluated_ids,
    }

    enough = (
        len(observed) >= MIN_SHADOW_LABELS
        and sum(observed) >= MIN_SHADOW_CLASS
        and len(observed) - sum(observed) >= MIN_SHADOW_CLASS
    )
    if not enough:
        return {
            "decision": "wait",
            "reason": (
                f"Теневая проверка: {len(observed)}/{MIN_SHADOW_LABELS} новых исходов; "
                f"нужно не менее {MIN_SHADOW_CLASS} каждого класса"
            ),
            "metrics": metrics,
        }

    cand_auc = float(candidate_metrics["roc_auc"])
    base_auc = float(comparator_metrics["roc_auc"])
    cand_brier = float(candidate_metrics["brier"])
    base_brier = float(comparator_metrics["brier"])
    auc_not_worse = cand_auc >= base_auc
    brier_not_worse = cand_brier <= base_brier
    demonstrably_better = (
        cand_auc >= base_auc + MIN_AUC_IMPROVEMENT
        or cand_brier <= base_brier - MIN_BRIER_IMPROVEMENT
    )
    passed = (
        cand_auc >= MIN_CV_AUC
        and auc_not_worse
        and brier_not_worse
        and demonstrably_better
    )
    return {
        "decision": "promote" if passed else "reject",
        "reason": (
            "Кандидат превзошёл действующий метод на будущих исходах"
            if passed else
            "Кандидат не доказал превосходство над действующим методом на будущих исходах"
        ),
        "metrics": metrics,
    }


def calculate_retraining(prepared: dict[str, Any], *, owner_node_id: str) -> dict[str, Any]:
    """CPU-only stage; safe to run with no database connection checked out."""
    rows = list(prepared.get("rows") or [])
    shadow = prepared.get("shadow")
    active = prepared.get("active")
    evaluation = _shadow_evaluation(rows, shadow, active) if shadow else None

    # Keep the current candidate until future validation decides it. Once it is
    # promoted/rejected, train the next candidate on all currently known labels.
    should_train = shadow is None or (evaluation or {}).get("decision") in {"promote", "reject"}
    candidate = None
    rejection_reason = None
    if should_train:
        model = train_defect_model([(row["group"], int(row["label"])) for row in rows])
        if model is None:
            rejection_reason = (
                "Недостаточно разметки обоих классов либо кандидат не прошёл "
                f"последовательную кросс-валидацию AUC ≥ {MIN_CV_AUC}"
            )
        else:
            fingerprint = training_fingerprint(rows)
            candidate = {
                "model_name": MODEL_NAME,
                "algorithm": str(model.get("type") or "unknown"),
                "owner_node_id": owner_node_id,
                "training_fingerprint": fingerprint,
                "feature_schema_hash": feature_schema_hash(model),
                "training_session_ids": [str(row["session_id"]) for row in rows],
                "training_size": len(rows),
                "positive_count": int(sum(int(row["label"]) for row in rows)),
                "artifact": model,
                "metrics": {
                    "cv_auc": model.get("cv_auc"),
                    "cv_folds": model.get("cv_folds"),
                },
                "quality_gates": {
                    "cross_validation_passed": True,
                    "minimum_cv_auc": MIN_CV_AUC,
                    "future_shadow_validation_required": True,
                    "minimum_future_labels": MIN_SHADOW_LABELS,
                },
                "app_version": APP_VERSION,
                "analysis_version": ANALYSIS_VERSION,
                "git_sha": git_sha(),
                "config_hash": stable_hash({
                    "minimum_cv_auc": MIN_CV_AUC,
                    "minimum_shadow_labels": MIN_SHADOW_LABELS,
                    "minimum_shadow_class": MIN_SHADOW_CLASS,
                    "minimum_brier_improvement": MIN_BRIER_IMPROVEMENT,
                    "minimum_auc_improvement": MIN_AUC_IMPROVEMENT,
                }),
                "parent_model_version_id": (active or {}).get("model_version_id"),
            }
    return {
        "model_name": MODEL_NAME,
        "label_count": len(rows),
        "training_fingerprint": training_fingerprint(rows),
        "shadow_model_version_id": (shadow or {}).get("model_version_id"),
        "evaluation": evaluation,
        "candidate": candidate,
        "candidate_rejection_reason": rejection_reason,
    }


def persist_retraining(db: Session, result: dict[str, Any]) -> dict[str, Any]:
    registry = ModelRegistryRepository(db)
    evaluation = result.get("evaluation") or {}
    shadow_id = result.get("shadow_model_version_id")
    decision = evaluation.get("decision")
    if shadow_id and decision:
        updated = registry.record_shadow_evaluation(
            str(shadow_id),
            dict(evaluation.get("metrics") or {}),
            decision_reason=str(evaluation.get("reason") or decision),
        )
        if updated is not None and decision == "promote":
            registry.promote(str(shadow_id), str(evaluation["reason"]))
        elif updated is not None and decision == "reject":
            registry.reject(str(shadow_id), str(evaluation["reason"]))

    candidate = result.get("candidate")
    registered = registry.register_shadow(candidate) if candidate else None
    return {
        **{key: value for key, value in result.items() if key != "candidate"},
        "candidate_model_version_id": (registered or {}).get("model_version_id"),
        "candidate_status": (registered or {}).get("status"),
    }


__all__ = [
    "MODEL_NAME", "JOB_TYPE", "enqueue_retraining", "enqueue_retraining_for_session",
    "active_model",
    "prepare_retraining", "calculate_retraining", "persist_retraining",
    "training_fingerprint", "feature_schema_hash",
]
