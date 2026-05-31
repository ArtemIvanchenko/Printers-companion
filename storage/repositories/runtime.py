from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.enums.common import VerificationStatus
from domain.models.entities import (
    BuildSession,
    CanonicalEvent,
    ConfirmedKnowledge,
    HistoricalAnalysisVerdict,
    ImportJob,
    NotificationOutbox,
    OperatorEvent,
    OperatorJournalEntry,
    PatternInsight,
    QualityOutcome,
    ReportArtifact,
    SourceFile,
)
from domain.services.import_jobs import ImportJobRecord
from domain.services.ingestion import IngestedFile
from operator_journal.notifications import NotificationMessage


def _model_to_dict(row: Any, fields: list[str]) -> dict[str, Any]:
    """Generic model to dict converter."""
    return jsonable_encoder({field: getattr(row, field, None) for field in fields})


class RuntimeRepository:
    """Persistence boundary for API/runtime workflows.

    Heavy parser outputs are kept as JSON payloads on the session/report rows until
    the deeper normalized repositories are wired end-to-end.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def commit(self) -> None:
        self.db.commit()

    def _upsert(self, entity_class, entity_id: str, id_field: str, values: dict[str, Any]) -> Any:
        """Helper for save-or-update pattern - reduces boilerplate."""
        existing = self.db.get(entity_class, entity_id)
        if existing:
            for key, value in values.items():
                setattr(existing, key, value)
            return existing
        else:
            entity = entity_class(**{id_field: entity_id, **values})
            self.db.add(entity)
            return entity

    def save_import_job(self, job: ImportJobRecord) -> None:
        data = job.model_dump()
        values = {
            "source_path": job.source_path,
            "source_name": job.source_name,
            "source_kind": job.source_kind,
            "status": job.status.value,
            "detected_at": job.detected_at,
            "updated_at": job.updated_at,
            "confirmation_deadline": job.confirmation_deadline,
            "confirmed_by": job.confirmed_by,
            "confirmed_at": job.confirmed_at,
            "postponed_until": job.postponed_until,
            "ignored_by": job.ignored_by,
            "ignored_at": job.ignored_at,
            "last_stability_check_at": job.last_stability_check_at,
            "file_snapshot": jsonable_encoder(data["file_snapshot"]),
            "checksum_manifest": jsonable_encoder(data["checksum_manifest"]),
            "session_ids": jsonable_encoder(data["session_ids"]),
            "report_ids": jsonable_encoder(data["report_ids"]),
            "missing_context_questions": jsonable_encoder(data["missing_context_questions"]),
            "notification_log": jsonable_encoder(data["notification_log"]),
            "error": job.error,
            "audit_trail": jsonable_encoder(data["audit_trail"]),
        }
        self._upsert(ImportJob, job.import_job_id, "import_job_id", values)

    def get_import_job(self, import_job_id: str) -> ImportJobRecord | None:
        row = self.db.get(ImportJob, import_job_id)
        if not row:
            return None
        return ImportJobRecord.model_validate(
            {
                "import_job_id": row.import_job_id,
                "source_path": row.source_path,
                "source_name": row.source_name,
                "source_kind": row.source_kind,
                "status": row.status,
                "detected_at": row.detected_at,
                "updated_at": row.updated_at,
                "confirmation_deadline": row.confirmation_deadline,
                "confirmed_by": row.confirmed_by,
                "confirmed_at": row.confirmed_at,
                "postponed_until": row.postponed_until,
                "ignored_by": row.ignored_by,
                "ignored_at": row.ignored_at,
                "last_stability_check_at": row.last_stability_check_at,
                "file_snapshot": row.file_snapshot or {},
                "checksum_manifest": row.checksum_manifest or {},
                "session_ids": row.session_ids or [],
                "report_ids": row.report_ids or [],
                "missing_context_questions": row.missing_context_questions or [],
                "notification_log": row.notification_log or [],
                "error": row.error,
                "audit_trail": row.audit_trail or [],
            }
        )

    def list_import_jobs(self) -> list[ImportJobRecord]:
        rows = self.db.scalars(select(ImportJob).order_by(ImportJob.detected_at.desc())).all()
        return [
            ImportJobRecord.model_validate(
                {
                    "import_job_id": row.import_job_id,
                    "source_path": row.source_path,
                    "source_name": row.source_name,
                    "source_kind": row.source_kind,
                    "status": row.status,
                    "detected_at": row.detected_at,
                    "updated_at": row.updated_at,
                    "confirmation_deadline": row.confirmation_deadline,
                    "confirmed_by": row.confirmed_by,
                    "confirmed_at": row.confirmed_at,
                    "postponed_until": row.postponed_until,
                    "ignored_by": row.ignored_by,
                    "ignored_at": row.ignored_at,
                    "last_stability_check_at": row.last_stability_check_at,
                    "file_snapshot": row.file_snapshot or {},
                    "checksum_manifest": row.checksum_manifest or {},
                    "session_ids": row.session_ids or [],
                    "report_ids": row.report_ids or [],
                    "missing_context_questions": row.missing_context_questions or [],
                    "notification_log": row.notification_log or [],
                    "error": row.error,
                    "audit_trail": row.audit_trail or [],
                }
            )
            for row in rows
        ]

    def save_notifications(self, notifications: Iterable[NotificationMessage]) -> None:
        for notification in notifications:
            existing = self.db.get(NotificationOutbox, notification.notification_id)
            values = notification.model_dump(mode="json")
            if existing:
                existing.channel = values["channel"]
                existing.text = values["text"]
                existing.buttons = values["buttons"]
                existing.metadata_json = values["metadata"]
            else:
                self.db.add(
                    NotificationOutbox(
                        notification_id=values["notification_id"],
                        channel=values["channel"],
                        text=values["text"],
                        buttons=values["buttons"],
                        metadata_json=values["metadata"],
                        created_at=notification.created_at,
                    )
                )

    def list_pending_notifications(self, channel: str = "telegram", limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(NotificationOutbox)
            .where(NotificationOutbox.channel == channel, NotificationOutbox.status == "pending")
            .order_by(NotificationOutbox.created_at.asc())
            .limit(limit)
        ).all()
        return [
            jsonable_encoder(
                {
                    "notification_id": row.notification_id,
                    "channel": row.channel,
                    "text": row.text,
                    "buttons": row.buttons or [],
                    "metadata": row.metadata_json or {},
                    "created_at": row.created_at,
                    "status": row.status,
                }
            )
            for row in rows
        ]

    def mark_notification_sent(self, notification_id: str, status_value: str = "sent", error: str | None = None) -> bool:
        row = self.db.get(NotificationOutbox, notification_id)
        if not row:
            return False
        row.status = status_value
        row.sent_at = datetime.now(timezone.utc) if status_value == "sent" else row.sent_at
        row.error = error
        return True

    def save_session_payload(self, session_id: str, payload: dict[str, Any]) -> None:
        existing = self.db.get(BuildSession, session_id)
        context = {"runtime_payload": jsonable_encoder(payload)}
        if existing:
            existing.context = context
            existing.updated_at = datetime.now(timezone.utc)
        else:
            self.db.add(
                BuildSession(
                    session_id=session_id,
                    status="runtime_payload",
                    context=context,
                    grouping_confidence=float(payload.get("group", {}).get("confidence") or 0.0),
                )
            )
        self.commit()

    def save_sessions(self, sessions: dict[str, dict[str, Any]]) -> None:
        for session_id, payload in sessions.items():
            self.save_session_payload(session_id, payload)

    def get_session_payload(self, session_id: str) -> dict[str, Any] | None:
        row = self.db.get(BuildSession, session_id)
        if not row:
            return None
        return (row.context or {}).get("runtime_payload")

    def list_session_payloads(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self.db.scalars(select(BuildSession).order_by(BuildSession.created_at.desc())).all()
        payloads: list[tuple[str, dict[str, Any]]] = []
        for row in rows:
            payload = (row.context or {}).get("runtime_payload")
            if payload:
                payloads.append((row.session_id, payload))
        return payloads

    def get_session_files(self, session_id: str) -> list[IngestedFile] | None:
        payload = self.get_session_payload(session_id)
        if not payload:
            return None
        return [IngestedFile.model_validate(item) for item in payload.get("files", [])]

    def save_report(self, report: dict[str, Any], report_type: str = "session") -> None:
        report_id = report["report_id"]
        values = {
            "session_id": report.get("session_id"),
            "report_type": report_type,
            "payload": jsonable_encoder(report),
            "version_metadata": jsonable_encoder(report.get("version_metadata", {})),
        }
        self._upsert(ReportArtifact, report_id, "report_id", values)
        self.commit()

    def save_reports(self, reports: dict[str, dict[str, Any]]) -> None:
        for report in reports.values():
            self.save_report(report)

    def get_report(self, report_id: str) -> dict[str, Any] | None:
        row = self.db.get(ReportArtifact, report_id)
        return row.payload if row else None

    def list_reports_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(ReportArtifact).where(ReportArtifact.session_id == session_id).order_by(ReportArtifact.generated_at.desc())
        ).all()
        return [row.payload for row in rows]

    def save_operator_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event = jsonable_encoder(event)
        event_id = event["event_id"]
        timestamp = _parse_datetime(event.get("timestamp")) or datetime.now(timezone.utc)
        values = {
            "timestamp": timestamp,
            "created_by": event.get("created_by", "unknown"),
            "source_channel": event.get("source_channel", "api"),
            "event_type": event.get("event_type", "operator_observation"),
            "printer_id": event.get("printer_id"),
            "session_id": event.get("session_id"),
            "build_id": event.get("build_id"),
            "layer": event.get("layer"),
            "material": event.get("material"),
            "powder_batch": event.get("powder_batch"),
            "gas_type": event.get("gas_type"),
            "gas_cylinder_id": event.get("gas_cylinder_id"),
            "component": event.get("component"),
            "action": event.get("action"),
            "value": event.get("value"),
            "unit": event.get("unit"),
            "note": event.get("note"),
            "attachments": event.get("attachments", []),
            "confidence": float(event.get("confidence", 0.5)),
            "verification_status": event.get("verification_status", VerificationStatus.unverified.value),
            "linked_machine_events": event.get("linked_machine_events", []),
            "audit_trail": event.get("audit_trail", []),
        }
        self._upsert(OperatorEvent, event_id, "event_id", values)
        self.commit()
        return event

    def get_operator_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.db.get(OperatorEvent, event_id)
        return _operator_event_to_dict(row) if row else None

    def list_operator_events(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(OperatorEvent).order_by(OperatorEvent.timestamp.desc())).all()
        return [_operator_event_to_dict(row) for row in rows]

    def save_operator_journal_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry = jsonable_encoder(entry)
        entry_id = entry["journal_entry_id"]
        values = {
            "created_at": _parse_datetime(entry.get("created_at")) or datetime.now(timezone.utc),
            "source_channel": entry.get("source_channel", "telegram"),
            "created_by": entry.get("created_by", "unknown"),
            "printer_id": entry.get("printer_id"),
            "session_id": entry.get("session_id"),
            "project_id": entry.get("project_id"),
            "platform_id": entry.get("platform_id"),
            "duplication_group_id": entry.get("duplication_group_id"),
            "entry_kind": entry.get("entry_kind", "operator_input"),
            "raw_text": entry.get("raw_text"),
            "normalized_text": entry.get("normalized_text"),
            "voice_attachment": entry.get("voice_attachment"),
            "transcription": entry.get("transcription", {}),
            "operator_event_id": entry.get("operator_event_id"),
            "status": entry.get("status", "draft"),
            "duplicate_targets": entry.get("duplicate_targets", []),
            "export_payload": entry.get("export_payload", {}),
            "audit_trail": entry.get("audit_trail", []),
        }
        self._upsert(OperatorJournalEntry, entry_id, "journal_entry_id", values)
        return entry

    def get_operator_journal_entry(self, journal_entry_id: str) -> dict[str, Any] | None:
        row = self.db.get(OperatorJournalEntry, journal_entry_id)
        return _operator_journal_entry_to_dict(row) if row else None

    def list_operator_journal_entries(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(OperatorJournalEntry).order_by(OperatorJournalEntry.created_at.desc())).all()
        return [_operator_journal_entry_to_dict(row) for row in rows]

    def save_quality_outcome(self, outcome: dict[str, Any]) -> dict[str, Any]:
        outcome = jsonable_encoder(outcome)
        outcome_id = outcome["outcome_id"]
        values = {
            "session_id": outcome.get("session_id"),
            "build_id": outcome.get("build_id"),
            "part_id": outcome.get("part_id"),
            "timestamp": _parse_datetime(outcome.get("timestamp")) or datetime.now(timezone.utc),
            "inspection_type": outcome.get("inspection_type", "visual"),
            "result": outcome.get("result", "unknown"),
            "defect_type": outcome.get("defect_type"),
            "defect_location": outcome.get("defect_location"),
            "layer_range": outcome.get("layer_range"),
            "severity": outcome.get("severity"),
            "notes": outcome.get("notes"),
            "attachments": outcome.get("attachments", []),
            "created_by": outcome.get("created_by", "unknown"),
            "evidence_links": outcome.get("evidence_links", []),
        }
        self._upsert(QualityOutcome, outcome_id, "outcome_id", values)
        self.commit()
        return outcome

    def get_quality_outcome(self, outcome_id: str) -> dict[str, Any] | None:
        row = self.db.get(QualityOutcome, outcome_id)
        return _quality_outcome_to_dict(row) if row else None

    def list_quality_outcomes(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(QualityOutcome).order_by(QualityOutcome.timestamp.desc())).all()
        return [_quality_outcome_to_dict(row) for row in rows]

    def save_historical_verdict(self, verdict: dict[str, Any]) -> None:
        verdict = jsonable_encoder(verdict)
        verdict_id = verdict["verdict_id"]
        values = {
            "created_at": _parse_datetime(verdict.get("created_at")) or datetime.now(timezone.utc),
            "analysis_window": verdict.get("analysis_window", {}),
            "max_iterations": verdict.get("max_iterations", 10),
            "completed_iterations": verdict.get("completed_iterations", 0),
            "status": verdict.get("status", "completed"),
            "verdict": verdict.get("verdict", "no_new_pattern"),
            "confidence": verdict.get("confidence", 0.0),
            "summary": verdict.get("summary", ""),
            "new_insights": verdict.get("new_insights", []),
            "updated_insights": verdict.get("updated_insights", []),
            "dismissed_candidates": verdict.get("dismissed_candidates", []),
            "counterexamples": verdict.get("counterexamples", []),
            "missing_data": verdict.get("missing_data", []),
            "recommended_actions": verdict.get("recommended_actions", []),
            "affected_sessions": verdict.get("affected_sessions", []),
            "analysis_version": verdict.get("analysis_version", "0.1.0"),
            "evidence_links": verdict.get("evidence_links", []),
        }
        self._upsert(HistoricalAnalysisVerdict, verdict_id, "verdict_id", values)

    def list_historical_verdicts(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(HistoricalAnalysisVerdict).order_by(HistoricalAnalysisVerdict.created_at.desc())).all()
        return [_historical_verdict_to_dict(row) for row in rows]

    def get_historical_verdict(self, verdict_id: str) -> dict[str, Any] | None:
        row = self.db.get(HistoricalAnalysisVerdict, verdict_id)
        return _historical_verdict_to_dict(row) if row else None

    def save_insight(self, insight: dict[str, Any]) -> dict[str, Any]:
        insight = jsonable_encoder(insight)
        insight_id = insight["insight_id"]
        values = _pattern_insight_values(insight)
        self._upsert(PatternInsight, insight_id, "insight_id", values)
        return insight

    def get_insight(self, insight_id: str) -> dict[str, Any] | None:
        row = self.db.get(PatternInsight, insight_id)
        return _pattern_insight_to_dict(row) if row else None

    def list_insights(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(PatternInsight).order_by(PatternInsight.created_at.desc())).all()
        return [_pattern_insight_to_dict(row) for row in rows]

    def save_knowledge(self, item: dict[str, Any]) -> dict[str, Any]:
        item = jsonable_encoder(item)
        knowledge_id = item["knowledge_id"]
        values = _knowledge_values(item)
        self._upsert(ConfirmedKnowledge, knowledge_id, "knowledge_id", values)
        return item

    def get_knowledge(self, knowledge_id: str) -> dict[str, Any] | None:
        row = self.db.get(ConfirmedKnowledge, knowledge_id)
        return _knowledge_to_dict(row) if row else None

    def list_knowledge(self) -> list[dict[str, Any]]:
        rows = self.db.scalars(select(ConfirmedKnowledge).order_by(ConfirmedKnowledge.confirmed_at.desc())).all()
        return [_knowledge_to_dict(row) for row in rows]

    def save_source_file(self, source_file_id: str, session_id: str | None, 
                         file_name: str, checksum: str, original_path: str,
                         size_bytes: int, family: str, role: str, 
                         encoding: str | None = None, data_quality_status: str = "ok",
                         first_ts: datetime | None = None, last_ts: datetime | None = None,
                         metadata: dict[str, Any] | None = None) -> SourceFile:
        """Save a source file record to the database."""
        values = {
            "session_id": session_id,
            "original_path": original_path,
            "file_name": file_name,
            "checksum": checksum,
            "size_bytes": size_bytes,
            "family": family,
            "role": role,
            "encoding": encoding,
            "data_quality_status": data_quality_status,
            "first_ts": first_ts,
            "last_ts": last_ts,
            "parse_status": "parsed",
            "metadata_json": metadata or {},
        }
        result = self._upsert(SourceFile, source_file_id, "source_file_id", values)
        self.db.flush()
        return result

    def save_canonical_event(self, event_id: str, session_id: str | None,
                            source_file_id: str | None, ts: datetime | None,
                            raw_timestamp: str | None, event_type: str,
                            subsystem: str | None = None, phase: str | None = None,
                            severity: str = "info", confidence: float = 1.0,
                            layer: int | None = None, source_line: int | None = None,
                            source_offset: int | None = None, raw_excerpt: str | None = None,
                            payload: dict[str, Any] | None = None,
                            evidence_kind: str = "machine_log",
                            provenance: list[dict[str, Any]] | None = None) -> CanonicalEvent:
        """Save a canonical event to the database."""
        values = {
            "session_id": session_id,
            "source_file_id": source_file_id,
            "ts": ts,
            "raw_timestamp": raw_timestamp,
            "ts_uncertainty": 0.0,
            "layer": layer,
            "source_line": source_line,
            "source_offset": source_offset,
            "raw_excerpt": raw_excerpt,
            "subsystem": subsystem,
            "phase": phase,
            "event_type": event_type,
            "severity": severity,
            "confidence": confidence,
            "payload": payload or {},
            "evidence_kind": evidence_kind,
            "provenance": provenance or [{"source": "parser"}],
        }
        return self._upsert(CanonicalEvent, event_id, "event_id", values)

    def list_canonical_events_by_session(self, session_id: str) -> list[CanonicalEvent]:
        """Get all canonical events for a session."""
        return self.db.scalars(
            select(CanonicalEvent)
            .where(CanonicalEvent.session_id == session_id)
            .order_by(CanonicalEvent.ts)
        ).all()

    def list_source_files_by_session(self, session_id: str) -> list[SourceFile]:
        """Get all source files for a session."""
        return self.db.scalars(
            select(SourceFile)
            .where(SourceFile.session_id == session_id)
            .order_by(SourceFile.created_at)
        ).all()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _operator_event_to_dict(row: OperatorEvent) -> dict[str, Any]:
    return _model_to_dict(row, [
        "event_id", "timestamp", "created_at", "created_by", "source_channel",
        "event_type", "printer_id", "session_id", "build_id", "layer",
        "material", "powder_batch", "gas_type", "gas_cylinder_id",
        "component", "action", "value", "unit", "note", "attachments",
        "confidence", "verification_status", "linked_machine_events", "audit_trail"
    ])


def _operator_journal_entry_to_dict(row: OperatorJournalEntry) -> dict[str, Any]:
    return _model_to_dict(row, [
        "journal_entry_id", "created_at", "updated_at", "source_channel",
        "created_by", "printer_id", "session_id", "project_id", "platform_id",
        "duplication_group_id", "entry_kind", "raw_text", "normalized_text",
        "voice_attachment", "transcription", "operator_event_id", "status",
        "duplicate_targets", "export_payload", "audit_trail"
    ])


def _quality_outcome_to_dict(row: QualityOutcome) -> dict[str, Any]:
    return _model_to_dict(row, [
        "outcome_id", "session_id", "build_id", "part_id", "timestamp",
        "inspection_type", "result", "defect_type", "defect_location",
        "layer_range", "severity", "notes", "attachments", "created_by", "evidence_links"
    ])


def _historical_verdict_to_dict(row: HistoricalAnalysisVerdict) -> dict[str, Any]:
    return _model_to_dict(row, [
        "verdict_id", "created_at", "analysis_window", "max_iterations",
        "completed_iterations", "status", "verdict", "confidence", "summary",
        "new_insights", "updated_insights", "dismissed_candidates",
        "counterexamples", "missing_data", "recommended_actions",
        "affected_sessions", "analysis_version", "evidence_links"
    ])


def _pattern_insight_values(insight: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": _parse_datetime(insight.get("created_at")) or datetime.now(timezone.utc),
        "updated_at": _parse_datetime(insight.get("updated_at")) or datetime.now(timezone.utc),
        "analysis_window": insight.get("analysis_window", {}),
        "printer_id": insight.get("printer_id"),
        "scope_filters": insight.get("scope_filters", {}),
        "insight_type": insight.get("insight_type", "manual"),
        "title": insight.get("title", "Untitled insight"),
        "description": insight.get("description", ""),
        "supporting_sessions": insight.get("supporting_sessions", []),
        "supporting_events": insight.get("supporting_events", []),
        "counterexamples": insight.get("counterexamples", []),
        "sample_size": insight.get("sample_size", 0),
        "effect_size": insight.get("effect_size"),
        "confidence": insight.get("confidence", 0.0),
        "causal_data_quality": insight.get("causal_data_quality", {}),
        "status": insight.get("status", "draft"),
        "generated_by": insight.get("generated_by", "system"),
        "analysis_version": insight.get("analysis_version", "0.1.0"),
        "recommended_action": insight.get("recommended_action"),
        "audit_trail": insight.get("audit_trail", []),
    }


def _pattern_insight_to_dict(row: PatternInsight) -> dict[str, Any]:
    return _model_to_dict(row, [
        "insight_id", "created_at", "updated_at", "analysis_window",
        "printer_id", "scope_filters", "insight_type", "title", "description",
        "supporting_sessions", "supporting_events", "counterexamples",
        "sample_size", "effect_size", "confidence", "causal_data_quality",
        "status", "generated_by", "analysis_version", "recommended_action", "audit_trail"
    ])


def _knowledge_values(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title", "Untitled knowledge"),
        "description": item.get("description", ""),
        "scope": item.get("scope", {}),
        "printer_profile": item.get("printer_profile"),
        "applicable_materials": item.get("applicable_materials", []),
        "applicable_conditions": item.get("applicable_conditions", {}),
        "supporting_insights": item.get("supporting_insights", []),
        "confirmed_by": item.get("confirmed_by", "system"),
        "confirmed_at": _parse_datetime(item.get("confirmed_at")) or datetime.now(timezone.utc),
        "confidence": item.get("confidence", 0.0),
        "status": item.get("status", "active"),
        "rule_implications": item.get("rule_implications", {}),
        "report_implications": item.get("report_implications", {}),
        "audit_trail": item.get("audit_trail", []),
    }


def _knowledge_to_dict(row: ConfirmedKnowledge) -> dict[str, Any]:
    return _model_to_dict(row, [
        "knowledge_id", "title", "description", "scope", "printer_profile",
        "applicable_materials", "applicable_conditions", "supporting_insights",
        "confirmed_by", "confirmed_at", "confidence", "status",
        "rule_implications", "report_implications", "audit_trail"
    ])
