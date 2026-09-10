"""Persistent registry for locally trained analytical models.

The registry lives in the shared database, but training and evaluation are
performed by an operator workstation.  Rows are deliberately small JSON
artifacts/metrics; the NAS never executes model code.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from domain.models.sessions import _new_id, utcnow
from storage.db.base import Base
from storage.db.session import _json_default_dict, _json_default_list


class MLModelVersion(Base):
    """One immutable training result plus its promotion lifecycle."""

    __tablename__ = "ml_model_versions"
    __table_args__ = (
        UniqueConstraint(
            "model_name",
            "training_fingerprint",
            name="uq_ml_model_versions_name_training_fingerprint",
        ),
        Index("ix_ml_model_versions_name_status", "model_name", "status"),
        Index(
            "ux_ml_model_versions_one_active",
            "model_name",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
        Index(
            "ux_ml_model_versions_one_shadow",
            "model_name",
            unique=True,
            postgresql_where=text("status = 'shadow'"),
            sqlite_where=text("status = 'shadow'"),
        ),
    )

    model_version_id: Mapped[str] = mapped_column(
        String(80), primary_key=True, default=lambda: _new_id("model")
    )
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="shadow")
    owner_node_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    training_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    feature_schema_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    training_session_ids: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    training_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    artifact_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    quality_gates_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    shadow_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    app_version: Mapped[str] = mapped_column(String(80), nullable=False)
    analysis_version: Mapped[str] = mapped_column(String(80), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(80))
    config_hash: Mapped[str | None] = mapped_column(String(64))
    parent_model_version_id: Mapped[str | None] = mapped_column(String(80))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
