from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from domain.enums.common import SourceChannel


def new_journal_entry_id() -> str:
    return f"op_journal_{uuid4().hex}"


def build_operator_journal_entry(
    *,
    source_channel: str = SourceChannel.telegram.value,
    created_by: str,
    entry_kind: str,
    raw_text: str | None = None,
    normalized_text: str | None = None,
    voice_attachment: dict[str, Any] | None = None,
    transcription: dict[str, Any] | None = None,
    operator_event_id: str | None = None,
    status: str = "draft",
    project_id: str | None = None,
    platform_id: str | None = None,
    duplicate_targets: list[dict[str, Any]] | None = None,
    audit_trail: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    entry = {
        "journal_entry_id": new_journal_entry_id(),
        "created_at": created_at,
        "updated_at": created_at,
        "source_channel": source_channel,
        "created_by": created_by,
        "project_id": project_id,
        "platform_id": platform_id,
        "duplication_group_id": f"journal_dup_{uuid4().hex}",
        "entry_kind": entry_kind,
        "raw_text": raw_text,
        "normalized_text": normalized_text,
        "voice_attachment": voice_attachment,
        "transcription": transcription or {},
        "operator_event_id": operator_event_id,
        "status": status,
        "duplicate_targets": duplicate_targets or [],
        "audit_trail": audit_trail or [],
    }
    entry["export_payload"] = build_export_payload(entry)
    return entry


def build_export_payload(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "operator_journal_entry.v1",
        "journal_entry_id": entry["journal_entry_id"],
        "duplication_group_id": entry.get("duplication_group_id"),
        "created_at": entry.get("created_at"),
        "source_channel": entry.get("source_channel"),
        "created_by": entry.get("created_by"),
        "project_id": entry.get("project_id"),
        "platform_id": entry.get("platform_id"),
        "entry_kind": entry.get("entry_kind"),
        "raw_text": entry.get("raw_text"),
        "normalized_text": entry.get("normalized_text"),
        "voice_attachment": entry.get("voice_attachment"),
        "transcription": entry.get("transcription") or {},
        "operator_event_id": entry.get("operator_event_id"),
        "status": entry.get("status"),
    }
