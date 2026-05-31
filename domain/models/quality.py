"""Quality and maintenance models."""
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from storage.db.base import Base
from storage.db.session import _json_default_dict, _json_default_list


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class QualityOutcome(Base):
    __tablename__ = "quality_outcomes"

    outcome_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("quality"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    build_id: Mapped[str | None] = mapped_column(ForeignKey("build_jobs.build_id"), index=True)
    part_id: Mapped[str | None] = mapped_column(ForeignKey("parts.part_id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    inspection_type: Mapped[str] = mapped_column(String(80), index=True)
    result: Mapped[str] = mapped_column(String(80), index=True)
    defect_type: Mapped[str | None] = mapped_column(String(120), index=True)
    defect_location: Mapped[str | None] = mapped_column(String(240))
    layer_range: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    severity: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    created_by: Mapped[str] = mapped_column(String(120), default="unknown")
    evidence_links: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class Anomaly(Base):
    __tablename__ = "anomalies"

    anomaly_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("anomaly"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    ts_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ts_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    layer_start: Mapped[int | None] = mapped_column(Integer)
    layer_end: Mapped[int | None] = mapped_column(Integer)
    anomaly_type: Mapped[str] = mapped_column(String(160), index=True)
    severity: Mapped[str] = mapped_column(String(80), default="warning")
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    status: Mapped[str] = mapped_column(String(80), default="active")


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"

    maintenance_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("maintenance"))
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    component: Mapped[str] = mapped_column(String(160), index=True)
    action: Mapped[str] = mapped_column(String(160), index=True)
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("operator_events.event_id"))
    notes: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class ComponentStateTimeline(Base):
    __tablename__ = "component_state_timeline"

    state_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("component_state"))
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    component: Mapped[str] = mapped_column(String(160), index=True)
    state: Mapped[str] = mapped_column(String(160), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("operator_events.event_id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class GasCylinder(Base):
    __tablename__ = "gas_cylinders"

    gas_cylinder_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    gas_type: Mapped[str] = mapped_column(String(80), index=True)
    installed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    initial_pressure: Mapped[float | None] = mapped_column(Float)
    pressure_unit: Mapped[str | None] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class MaterialBatch(Base):
    __tablename__ = "material_batches"

    material_batch_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("material"))
    material: Mapped[str] = mapped_column(String(120), index=True)
    alloy: Mapped[str | None] = mapped_column(String(120), index=True)
    batch_code: Mapped[str] = mapped_column(String(160), index=True)
    supplier: Mapped[str | None] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class PowderUsageCycle(Base):
    __tablename__ = "powder_usage_cycles"

    powder_cycle_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("powder_cycle"))
    material_batch_id: Mapped[str | None] = mapped_column(ForeignKey("material_batches.material_batch_id"), index=True)
    powder_batch: Mapped[str | None] = mapped_column(String(160), index=True)
    reuse_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class PowderPreparationEvent(Base):
    __tablename__ = "powder_preparation_events"

    prep_event_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("powder_prep"))
    powder_cycle_id: Mapped[str | None] = mapped_column(ForeignKey("powder_usage_cycles.powder_cycle_id"), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str | None] = mapped_column(String(240))
    unit: Mapped[str | None] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class Attachment(Base):
    __tablename__ = "attachments"

    attachment_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("attachment"))
    owner_type: Mapped[str] = mapped_column(String(80), index=True)
    owner_id: Mapped[str] = mapped_column(String(80), index=True)
    file_type: Mapped[str | None] = mapped_column(String(120))
    storage_uri: Mapped[str] = mapped_column(String(700), nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(120), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    description: Mapped[str | None] = mapped_column(Text)
    hash: Mapped[str | None] = mapped_column(String(128))