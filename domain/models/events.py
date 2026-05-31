"""Event and logging models."""
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


class OperatorEvent(Base):
    __tablename__ = "operator_events"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("op_event"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(120), default="unknown")
    source_channel: Mapped[str] = mapped_column(String(40), index=True)
    event_type: Mapped[str] = mapped_column(String(120), index=True)
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    build_id: Mapped[str | None] = mapped_column(ForeignKey("build_jobs.build_id"), index=True)
    layer: Mapped[int | None] = mapped_column(Integer)
    material: Mapped[str | None] = mapped_column(String(120))
    powder_batch: Mapped[str | None] = mapped_column(String(160))
    gas_type: Mapped[str | None] = mapped_column(String(80))
    gas_cylinder_id: Mapped[str | None] = mapped_column(String(160))
    component: Mapped[str | None] = mapped_column(String(160))
    action: Mapped[str | None] = mapped_column(String(160))
    value: Mapped[str | None] = mapped_column(String(240))
    unit: Mapped[str | None] = mapped_column(String(80))
    note: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    verification_status: Mapped[str] = mapped_column(String(80), default="draft", index=True)
    linked_machine_events: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class OperatorJournalEntry(Base):
    __tablename__ = "operator_journal_entries"
    __table_args__ = (Index("ix_operator_journal_created_project", "created_at", "project_id"),)

    journal_entry_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("op_journal"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    source_channel: Mapped[str] = mapped_column(String(40), index=True)
    created_by: Mapped[str] = mapped_column(String(120), default="unknown", index=True)
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    project_id: Mapped[str | None] = mapped_column(String(160), index=True)
    platform_id: Mapped[str | None] = mapped_column(String(160), index=True)
    duplication_group_id: Mapped[str | None] = mapped_column(String(120), index=True)
    entry_kind: Mapped[str] = mapped_column(String(80), default="operator_input", index=True)
    raw_text: Mapped[str | None] = mapped_column(Text)
    normalized_text: Mapped[str | None] = mapped_column(Text)
    voice_attachment: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    transcription: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    operator_event_id: Mapped[str | None] = mapped_column(ForeignKey("operator_events.event_id"), index=True)
    status: Mapped[str] = mapped_column(String(80), default="draft", index=True)
    duplicate_targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    export_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class CanonicalEvent(Base):
    __tablename__ = "canonical_events"
    __table_args__ = (Index("ix_canonical_events_session_ts", "session_id", "ts"),)

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("event"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    raw_timestamp: Mapped[str | None] = mapped_column(String(160))
    ts_uncertainty: Mapped[float] = mapped_column(Float, default=0.0)
    layer: Mapped[int | None] = mapped_column(Integer, index=True)
    source_file_id: Mapped[str | None] = mapped_column(ForeignKey("source_files.source_file_id"), index=True)
    source_line: Mapped[int | None] = mapped_column(Integer)
    source_offset: Mapped[int | None] = mapped_column(Integer)
    raw_excerpt: Mapped[str | None] = mapped_column(Text)
    subsystem: Mapped[str | None] = mapped_column(String(120), index=True)
    phase: Mapped[str | None] = mapped_column(String(120), index=True)
    event_type: Mapped[str] = mapped_column(String(160), index=True)
    severity: Mapped[str] = mapped_column(String(60), default="info")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    evidence_kind: Mapped[str] = mapped_column(String(80), index=True)
    provenance: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StateTransition(Base):
    __tablename__ = "state_transitions"
    __table_args__ = (Index("ix_state_transitions_session_ts", "session_id", "ts_start"),)

    transition_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("transition"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    ts_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    ts_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    changed_columns: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    previous_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    new_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    subsystem: Mapped[str | None] = mapped_column(String(120), index=True)
    source_file_id: Mapped[str | None] = mapped_column(ForeignKey("source_files.source_file_id"), index=True)
    source_offset_start: Mapped[int | None] = mapped_column(Integer)
    source_offset_end: Mapped[int | None] = mapped_column(Integer)
    raw_excerpt_sample: Mapped[str | None] = mapped_column(Text)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    profile_version: Mapped[str | None] = mapped_column(String(80))


class LayerSnapshot(Base):
    __tablename__ = "layer_snapshots"

    layer_snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("layer"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    layer: Mapped[int] = mapped_column(Integer, index=True)
    ts_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ts_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    features: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class Segment(Base):
    __tablename__ = "segments"

    segment_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("segment"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    phase: Mapped[str] = mapped_column(String(120), index=True)
    ts_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ts_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    layer_start: Mapped[int | None] = mapped_column(Integer)
    layer_end: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    notification_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("notification"))
    channel: Mapped[str] = mapped_column(String(80), default="telegram", index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    buttons: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    status: Mapped[str] = mapped_column(String(80), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ParseDiagnostic(Base):
    __tablename__ = "parse_diagnostics"

    diagnostic_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("diag"))
    source_file_id: Mapped[str | None] = mapped_column(ForeignKey("source_files.source_file_id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    parser_name: Mapped[str] = mapped_column(String(160), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(40), default="info")
    code: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source_line: Mapped[int | None] = mapped_column(Integer)
    source_offset: Mapped[int | None] = mapped_column(Integer)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperatorEventAuditRecord(Base):
    __tablename__ = "operator_event_audit_records"

    audit_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("op_audit"))
    event_id: Mapped[str] = mapped_column(ForeignKey("operator_events.event_id"), index=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    reason: Mapped[str | None] = mapped_column(Text)


class ProductionContextSnapshot(Base):
    __tablename__ = "production_context_snapshots"

    snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("ctx"))
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    context: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source_event_id: Mapped[str | None] = mapped_column(ForeignKey("operator_events.event_id"))
    conflict_flags: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class SignalDefinition(Base):
    __tablename__ = "signal_dictionary_entries"

    signal_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    profile_id: Mapped[str] = mapped_column(ForeignKey("printer_profiles.profile_id"), index=True)
    raw_field_name: Mapped[str] = mapped_column(String(160), index=True)
    canonical_name: Mapped[str | None] = mapped_column(String(160), index=True)
    subsystem: Mapped[str | None] = mapped_column(String(120), index=True)
    semantic_class: Mapped[str | None] = mapped_column(String(120), index=True)
    unit: Mapped[str | None] = mapped_column(String(80))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    active_status: Mapped[str] = mapped_column(String(40), default="candidate")
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(120), default="profile_seed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    version: Mapped[str] = mapped_column(String(80), default="0.1.0")


class UnknownSignalReport(Base):
    __tablename__ = "unknown_signal_reports"

    report_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("unknown_signal"))
    field_name: Mapped[str] = mapped_column(String(160), index=True)
    source_file_family: Mapped[str] = mapped_column(String(80), index=True)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=0)
    value_distribution: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    correlated_known_events: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    candidate_semantic_class: Mapped[str | None] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    examples: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    affected_sessions: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)