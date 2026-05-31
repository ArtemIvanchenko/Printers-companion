from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class EvidencePackage(BaseModel):
    package_version: str = "0.2.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_summary: dict[str, Any] = Field(default_factory=dict)
    file_inventory: list[dict[str, Any]] = Field(default_factory=list)
    data_quality_summary: dict[str, Any] = Field(default_factory=dict)
    timeline: list[dict[str, Any]] = Field(default_factory=list)
    phase_segments: list[dict[str, Any]] = Field(default_factory=list)
    anomalies: list[dict[str, Any]] = Field(default_factory=list)
    hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    operator_context: dict[str, Any] = Field(default_factory=dict)
    quality_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    known_unknowns: list[dict[str, Any]] = Field(default_factory=list)
    version_metadata: dict[str, Any] = Field(default_factory=dict)
    analytics_summary: dict[str, Any] = Field(default_factory=dict)


def compact_event(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": event.get("ts"),
        "layer": event.get("layer"),
        "subsystem": event.get("subsystem"),
        "phase": event.get("phase"),
        "event_type": event.get("event_type"),
        "severity": event.get("severity"),
        "confidence": event.get("confidence"),
        "evidence_kind": event.get("evidence_kind"),
        "source": {
            "source_file_id": event.get("source_file_id"),
            "source_line": event.get("source_line"),
            "source_offset": event.get("source_offset"),
        },
    }


def build_evidence_package(
    report_payload: dict[str, Any],
    analytics_summary: dict[str, Any] | None = None,
) -> EvidencePackage:
    return EvidencePackage(
        session_summary=report_payload.get("session_summary", {}),
        file_inventory=report_payload.get("file_inventory", []),
        data_quality_summary=report_payload.get("data_quality", {}),
        timeline=[compact_event(event) for event in report_payload.get("timeline", [])[:200]],
        phase_segments=report_payload.get("phase_segments", []),
        anomalies=report_payload.get("anomalies", []),
        hypotheses=report_payload.get("hypotheses", []),
        operator_context=report_payload.get("operator_context", {}),
        quality_outcomes=report_payload.get("quality_outcomes", []),
        known_unknowns=report_payload.get("known_unknowns", []),
        version_metadata=report_payload.get("version_metadata", {}),
        analytics_summary=analytics_summary or {},
    )

