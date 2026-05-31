"""Analytics and insights models."""
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


class PatternInsight(Base):
    __tablename__ = "pattern_insights"

    insight_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("insight"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    analysis_window: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    printer_id: Mapped[str | None] = mapped_column(ForeignKey("printers.printer_id"), index=True)
    scope_filters: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    insight_type: Mapped[str] = mapped_column(String(120), index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    supporting_sessions: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    supporting_events: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    counterexamples: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    effect_size: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    causal_data_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    status: Mapped[str] = mapped_column(String(80), default="draft", index=True)
    generated_by: Mapped[str] = mapped_column(String(120), default="background_reanalysis")
    analysis_version: Mapped[str] = mapped_column(String(80), default="0.1.0")
    recommended_action: Mapped[str | None] = mapped_column(Text)
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class ConfirmedKnowledge(Base):
    __tablename__ = "confirmed_knowledge"

    knowledge_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("knowledge"))
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    printer_profile: Mapped[str | None] = mapped_column(String(120), index=True)
    applicable_materials: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    applicable_conditions: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    supporting_insights: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    confirmed_by: Mapped[str] = mapped_column(String(120), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(80), default="active", index=True)
    rule_implications: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    report_implications: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    audit_trail: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class HistoricalAnalysisVerdict(Base):
    __tablename__ = "historical_analysis_verdicts"

    verdict_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("verdict"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    analysis_window: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    max_iterations: Mapped[int] = mapped_column(Integer, default=10)
    completed_iterations: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(80), index=True)
    verdict: Mapped[str] = mapped_column(String(80), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    summary: Mapped[str] = mapped_column(Text, default="")
    new_insights: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    updated_insights: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    dismissed_candidates: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    counterexamples: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    missing_data: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    recommended_actions: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    affected_sessions: Mapped[list[str]] = mapped_column(JSON, default=_json_default_list)
    analysis_version: Mapped[str] = mapped_column(String(80), default="0.1.0")
    evidence_links: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    hypothesis_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("hypothesis"))
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    relationship: Mapped[str] = mapped_column(String(80), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    uncertainty: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    supporting_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    contradictions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CausalLink(Base):
    __tablename__ = "causal_links"

    causal_link_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("causal"))
    source_id: Mapped[str] = mapped_column(String(80), index=True)
    target_id: Mapped[str] = mapped_column(String(80), index=True)
    relationship: Mapped[str] = mapped_column(String(80), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=_json_default_list)
    data_quality: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)


class LLMRun(Base):
    __tablename__ = "llm_runs"

    llm_run_id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: _new_id("llm_run"))
    report_id: Mapped[str | None] = mapped_column(ForeignKey("reports.report_id"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    model_name: Mapped[str] = mapped_column(String(160))
    prompt_version: Mapped[str] = mapped_column(String(80))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    token_estimates: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    success: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text)
    request_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)
    response_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=_json_default_dict)