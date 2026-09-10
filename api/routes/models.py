"""Operator-visible status and manual trigger for guarded ML models."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from api.deps.repositories import get_runtime_repository
from analytics.prediction.retraining import JOB_TYPE, MODEL_NAME
from core.config.settings import get_settings
from storage.repositories.jobs_repo import JobsRepository
from storage.repositories.model_registry import ModelRegistryRepository
from storage.repositories.runtime import RuntimeRepository

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/defect-risk")
def defect_risk_model_status(
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    from analytics.prediction.defect_risk import MIN_LABELS, MIN_LABELS_GBM
    from analytics.prediction.retraining import MIN_SHADOW_CLASS, MIN_SHADOW_LABELS

    registry = ModelRegistryRepository(repo.db)
    return {
        "model_name": MODEL_NAME,
        "active": registry.get_active(MODEL_NAME, include_artifact=False),
        "shadow": registry.get_shadow(MODEL_NAME, include_artifact=False),
        "history": registry.list(MODEL_NAME),
        "policy": {
            "training_location": "operator_pc",
            "nas_role": "metadata_storage_only",
            "shadow_validation_required": True,
            "automatic_promotion_only_after_superiority": True,
            "min_labels": MIN_LABELS,
            "min_labels_gbm": MIN_LABELS_GBM,
            "min_future_shadow_labels": MIN_SHADOW_LABELS,
            "min_future_shadow_class": MIN_SHADOW_CLASS,
        },
    }


@router.post("/defect-risk/retrain")
def request_defect_risk_retrain(
    repo: RuntimeRepository = Depends(get_runtime_repository),
) -> dict:
    node_id = get_settings().compute_node_id
    request_id = os.urandom(12).hex()
    job = JobsRepository(repo.db).enqueue(
        job_type=JOB_TYPE,
        owner_node_id=node_id,
        entity_type="ml_model",
        entity_id=MODEL_NAME,
        idempotency_key=f"{JOB_TYPE}:{node_id}:manual:{request_id}",
        payload={"owner_node_id": node_id, "manual": True},
        max_attempts=3,
    )
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "owner_node_id": node_id,
        "message": "Переобучение поставлено в локальную очередь этого ПК",
    }
