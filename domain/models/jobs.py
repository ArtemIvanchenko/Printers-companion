"""Durable background work pinned to the operator PC that created it."""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from domain.models.sessions import _new_id, utcnow
from storage.db.base import Base
from storage.db.session import _json_default_dict


class BackgroundJob(Base):
    """Restart-safe task with an expiring lease and idempotency key."""

    __tablename__ = "background_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_background_jobs_idempotency_key"),
        Index(
            "ix_background_jobs_claim",
            "owner_node_id",
            "job_type",
            "status",
            "available_at",
        ),
    )

    job_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("task"))
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_node_id: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ComputeNodeRegistration(Base):
    """Immutable pairing of a logical owner id with one physical workstation."""

    __tablename__ = "compute_node_registrations"
    __table_args__ = (
        UniqueConstraint("instance_id", name="uq_compute_node_registrations_instance_id"),
    )

    compute_node_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
