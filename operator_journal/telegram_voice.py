from datetime import datetime, timezone
from typing import Any

from domain.enums.common import SourceChannel, VerificationStatus


VOICE_EVENT_TYPES = {
    "powder": "operator_voice_material_context",
    "gas": "operator_voice_gas_context",
    "maintenance": "operator_voice_maintenance",
    "operation": "operator_voice_operation",
    "quality": "operator_voice_quality",
    "note": "operator_voice_note",
}


def build_voice_attachment(voice_metadata: dict[str, Any]) -> dict[str, Any]:
    file_id = voice_metadata["file_id"]
    return {
        "file_type": "telegram_voice",
        "storage_uri": f"telegram:file_id:{file_id}",
        "telegram_file_id": file_id,
        "telegram_file_unique_id": voice_metadata.get("file_unique_id"),
        "duration_sec": voice_metadata.get("duration"),
        "mime_type": voice_metadata.get("mime_type"),
        "file_size": voice_metadata.get("file_size"),
        "description": "Operator Telegram voice note",
    }


def build_transcription_audit(transcription: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "voice_transcribed",
        "provider": transcription.get("provider"),
        "model": transcription.get("model"),
        "language": transcription.get("language"),
        "success": transcription.get("success"),
        "error": transcription.get("error"),
        "at": datetime.now(timezone.utc).isoformat(),
    }


def build_voice_operator_event(
    *,
    voice_metadata: dict[str, Any],
    created_by: str,
    entry_kind: str | None = None,
) -> dict[str, Any]:
    event_type = VOICE_EVENT_TYPES.get(entry_kind or "note", VOICE_EVENT_TYPES["note"])
    context_label = f" Категория: {entry_kind}." if entry_kind else ""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
        "source_channel": SourceChannel.telegram.value,
        "event_type": event_type,
        "note": (
            "Голосовое сообщение оператора сохранено как первичное свидетельство."
            f"{context_label} Требуется расшифровка или подтверждение."
        ),
        "attachments": [build_voice_attachment(voice_metadata)],
        "confidence": 0.3,
        "verification_status": VerificationStatus.unverified.value,
        "needs_confirmation": True,
        "parse_warnings": ["Voice message has not been transcribed by the system."],
        "audit_trail": [
            {
                "action": "voice_received",
                "source": "telegram",
                "entry_kind": entry_kind,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        ],
    }
