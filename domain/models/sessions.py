"""Session-related database models."""
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from storage.db.base import Base
from storage.db.session import _json_default_dict, _json_default_list


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PrinterProfile(Base):
    __tablename__ = "printer_profiles"

    profile_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    vendor: Mapped[str] = mapped_column(String(120), nullable=False)
    model_family: Mapped[str] = mapped_column(String(120), nullable=False)
    legacy_names: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    current_version: Mapped[str] = mapped_column(String(80), nullable=False)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProfileVersion(Base):
    __tablename__ = "profile_versions"

    version_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("printer_profiles.profile_id"), index=True)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    mappings_hash: Mapped[str | None] = mapped_column(String(128))
    rules_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(120), default="system")


class Printer(Base):
    __tablename__ = "printers"

    printer_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("printer"))
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    vendor: Mapped[str] = mapped_column(String(120), nullable=False)
    model_family: Mapped[str] = mapped_column(String(120), nullable=False)
    profile_id: Mapped[str] = mapped_column(ForeignKey("printer_profiles.profile_id"), index=True)
    serial_number: Mapped[str | None] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BuildSession(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("session"))
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("printer_profiles.profile_id"), index=True)
    start_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    classification: Mapped[str] = mapped_column(String(80), default="INCOMPLETE_OR_UNKNOWN")
    classification_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    grouping_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(80), default="new")
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    analysis_version: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BuildJob(Base):
    __tablename__ = "build_jobs"

    build_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("build"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    job_name: Mapped[str | None] = mapped_column(String(240))
    recipe: Mapped[str | None] = mapped_column(String(240))
    layer_count: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class Part(Base):
    __tablename__ = "parts"

    part_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("part"))
    build_id: Mapped[str | None] = mapped_column(ForeignKey("build_jobs.build_id"), index=True)
    name: Mapped[str | None] = mapped_column(String(240))
    geometry_ref: Mapped[str | None] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class BuildPlate(Base):
    __tablename__ = "build_plates"

    plate_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("plate"))
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    identifier: Mapped[str | None] = mapped_column(String(160))
    material: Mapped[str | None] = mapped_column(String(120))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class PartPlacement(Base):
    __tablename__ = "part_placements"

    placement_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("placement"))
    part_id: Mapped[str] = mapped_column(ForeignKey("parts.part_id"), index=True)
    plate_id: Mapped[str | None] = mapped_column(ForeignKey("build_plates.plate_id"), index=True)
    x: Mapped[float | None] = mapped_column(Float)
    y: Mapped[float | None] = mapped_column(Float)
    rotation_deg: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class LayerRange(Base):
    __tablename__ = "layer_ranges"

    layer_range_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("layer_range"))
    start_layer: Mapped[int | None] = mapped_column(Integer)
    end_layer: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class SourceFile(Base):
    __tablename__ = "source_files"
    __table_args__ = (Index("ix_source_files_checksum", "checksum"),)

    source_file_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("file"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    object_uri: Mapped[str | None] = mapped_column(String(700))
    original_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    file_name: Mapped[str] = mapped_column(String(300), nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    family: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(40), index=True)
    encoding: Mapped[str | None] = mapped_column(String(80))
    data_quality_status: Mapped[str] = mapped_column(String(80), default="ok")
    first_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parse_status: Mapped[str] = mapped_column(String(80), default="pending")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (Index("ix_import_jobs_status_updated", "status", "updated_at"),)

    import_job_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("import"))
    source_path: Mapped[str] = mapped_column(String(1000), nullable=False, index=True)
    source_name: Mapped[str] = mapped_column(String(300), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(40), default="folder")
    status: Mapped[str] = mapped_column(String(80), default="detected", index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    confirmation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(120))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    postponed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignored_by: Mapped[str | None] = mapped_column(String(120))
    ignored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_stability_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    checksum_manifest: Mapped[dict[str, str]] = mapped_column(JSON, default=_json_default_dict)
    session_ids: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    report_ids: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    missing_context_questions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    notification_log: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    error: Mapped[str | None] = mapped_column(Text)
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class ReportArtifact(Base):
    __tablename__ = "reports"

    report_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("report"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    report_type: Mapped[str] = mapped_column(String(80), index=True)
    storage_uri: Mapped[str | None] = mapped_column(String(700))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    generated_by: Mapped[str] = mapped_column(String(120), default="system")
    version_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class AnalysisVersion(Base):
    __tablename__ = "analysis_versions"

    analysis_version_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("analysis_version"))
    component: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(80))
    config_hash: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ToleranceRule(Base):
    __tablename__ = "tolerance_rules"

    rule_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("rule"))
    feature_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(default=True)
    confirmed_by: Mapped[str] = mapped_column(String(120), default="operator")
    session_id_reference: Mapped[str | None] = mapped_column(String(80), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)