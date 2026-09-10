"""Persistence and atomic promotion for locally trained ML models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from domain.models.ml import MLModelVersion


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def model_to_dict(row: MLModelVersion, *, include_artifact: bool = False) -> dict[str, Any]:
    result = {
        "model_version_id": row.model_version_id,
        "model_name": row.model_name,
        "algorithm": row.algorithm,
        "status": row.status,
        "owner_node_id": row.owner_node_id,
        "training_fingerprint": row.training_fingerprint,
        "feature_schema_hash": row.feature_schema_hash,
        "training_session_ids": list(row.training_session_ids or []),
        "training_size": row.training_size,
        "positive_count": row.positive_count,
        "metrics": dict(row.metrics_json or {}),
        "quality_gates": dict(row.quality_gates_json or {}),
        "shadow_metrics": dict(row.shadow_metrics_json or {}),
        "app_version": row.app_version,
        "analysis_version": row.analysis_version,
        "git_sha": row.git_sha,
        "config_hash": row.config_hash,
        "parent_model_version_id": row.parent_model_version_id,
        "decision_reason": row.decision_reason,
        "created_at": _iso(row.created_at),
        "evaluated_at": _iso(row.evaluated_at),
        "activated_at": _iso(row.activated_at),
    }
    if include_artifact:
        result["artifact"] = dict(row.artifact_json or {})
    return result


class ModelRegistryRepository:
    """A small champion/challenger registry stored on the shared database.

    Only JSON model coefficients are persisted. Fitting/scoring is intentionally
    outside this class and therefore outside the NAS.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get(self, model_version_id: str, *, include_artifact: bool = False) -> dict[str, Any] | None:
        row = self.db.get(MLModelVersion, model_version_id)
        return model_to_dict(row, include_artifact=include_artifact) if row else None

    def get_active(self, model_name: str, *, include_artifact: bool = True) -> dict[str, Any] | None:
        row = self.db.scalar(
            select(MLModelVersion)
            .where(MLModelVersion.model_name == model_name, MLModelVersion.status == "active")
            .order_by(MLModelVersion.activated_at.desc(), MLModelVersion.created_at.desc())
        )
        return model_to_dict(row, include_artifact=include_artifact) if row else None

    def get_shadow(self, model_name: str, *, include_artifact: bool = True) -> dict[str, Any] | None:
        row = self.db.scalar(
            select(MLModelVersion)
            .where(MLModelVersion.model_name == model_name, MLModelVersion.status == "shadow")
            .order_by(MLModelVersion.created_at.desc())
        )
        return model_to_dict(row, include_artifact=include_artifact) if row else None

    def list(self, model_name: str, *, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(MLModelVersion)
            .where(MLModelVersion.model_name == model_name)
            .order_by(MLModelVersion.created_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        return [model_to_dict(row) for row in rows]

    def register_shadow(self, candidate: dict[str, Any]) -> dict[str, Any]:
        """Register one candidate, keeping an existing shadow undisturbed.

        A challenger needs future outcomes to prove itself. Replacing it after
        every new label would reset that validation window forever.
        """
        existing = self.get_shadow(str(candidate["model_name"]), include_artifact=True)
        if existing is not None:
            return existing

        row = MLModelVersion(
            model_name=str(candidate["model_name"]),
            algorithm=str(candidate["algorithm"]),
            status="shadow",
            owner_node_id=str(candidate["owner_node_id"]),
            training_fingerprint=str(candidate["training_fingerprint"]),
            feature_schema_hash=str(candidate["feature_schema_hash"]),
            training_session_ids=list(candidate.get("training_session_ids") or []),
            training_size=int(candidate.get("training_size") or 0),
            positive_count=int(candidate.get("positive_count") or 0),
            artifact_json=dict(candidate.get("artifact") or {}),
            metrics_json=dict(candidate.get("metrics") or {}),
            quality_gates_json=dict(candidate.get("quality_gates") or {}),
            app_version=str(candidate["app_version"]),
            analysis_version=str(candidate["analysis_version"]),
            git_sha=candidate.get("git_sha"),
            config_hash=candidate.get("config_hash"),
            parent_model_version_id=candidate.get("parent_model_version_id"),
            decision_reason=candidate.get("decision_reason") or "Ожидает проверки на будущих результатах",
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError:
            # Same dataset may be processed concurrently by two operator PCs.
            same = self.db.scalar(
                select(MLModelVersion).where(
                    MLModelVersion.model_name == candidate["model_name"],
                    MLModelVersion.training_fingerprint == candidate["training_fingerprint"],
                )
            )
            if same is None:
                existing = self.get_shadow(str(candidate["model_name"]), include_artifact=True)
                if existing is None:
                    raise
                return existing
            return model_to_dict(same, include_artifact=True)
        return model_to_dict(row, include_artifact=True)

    def record_shadow_evaluation(
        self,
        model_version_id: str,
        metrics: dict[str, Any],
        *,
        decision_reason: str,
    ) -> dict[str, Any] | None:
        row = self.db.scalar(
            select(MLModelVersion)
            .where(MLModelVersion.model_version_id == model_version_id)
            .with_for_update()
        )
        if row is None or row.status != "shadow":
            return None
        row.shadow_metrics_json = dict(metrics)
        row.evaluated_at = datetime.now(timezone.utc)
        row.decision_reason = decision_reason
        self.db.flush()
        return model_to_dict(row, include_artifact=True)

    def reject(self, model_version_id: str, reason: str) -> dict[str, Any] | None:
        row = self.db.scalar(
            select(MLModelVersion)
            .where(MLModelVersion.model_version_id == model_version_id)
            .with_for_update()
        )
        if row is None or row.status != "shadow":
            return None
        row.status = "rejected"
        row.decision_reason = reason
        row.evaluated_at = datetime.now(timezone.utc)
        self.db.flush()
        return model_to_dict(row)

    def promote(self, model_version_id: str, reason: str) -> dict[str, Any] | None:
        """Atomically replace the champion after shadow validation passes."""
        candidate = self.db.scalar(
            select(MLModelVersion)
            .where(MLModelVersion.model_version_id == model_version_id)
            .with_for_update()
        )
        if candidate is None or candidate.status != "shadow":
            return None

        # Serialise global promotion across operator PCs. This is metadata
        # coordination only; no numerical work is executed by PostgreSQL/NAS.
        if self.db.get_bind().dialect.name == "postgresql":
            self.db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:name))"),
                {"name": f"ml-promotion:{candidate.model_name}"},
            )

        current = self.db.scalar(
            select(MLModelVersion)
            .where(
                MLModelVersion.model_name == candidate.model_name,
                MLModelVersion.status == "active",
            )
            .with_for_update()
        )
        now = datetime.now(timezone.utc)
        if current is not None:
            current.status = "archived"
            current.decision_reason = f"Заменена моделью {candidate.model_version_id}"
            current.evaluated_at = now
            self.db.flush()
        candidate.status = "active"
        candidate.activated_at = now
        candidate.evaluated_at = now
        candidate.decision_reason = reason
        self.db.flush()
        return model_to_dict(candidate, include_artifact=True)
