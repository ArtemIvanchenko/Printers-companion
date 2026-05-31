from operator_journal.telegram_voice import build_voice_operator_event


def test_voice_event_preserves_telegram_attachment_metadata() -> None:
    event = build_voice_operator_event(
        voice_metadata={
            "file_id": "voice-file-123",
            "file_unique_id": "unique-voice-123",
            "duration": 14,
            "mime_type": "audio/ogg",
            "file_size": 4096,
        },
        created_by="telegram:42",
        entry_kind="gas",
    )

    assert event["event_type"] == "operator_voice_gas_context"
    assert event["source_channel"] == "telegram"
    assert event["created_by"] == "telegram:42"
    assert event["verification_status"] == "unverified"
    assert event["needs_confirmation"] is True
    assert event["attachments"][0]["file_type"] == "telegram_voice"
    assert event["attachments"][0]["storage_uri"] == "telegram:file_id:voice-file-123"
    assert event["attachments"][0]["duration_sec"] == 14


def test_unknown_voice_context_falls_back_to_operator_voice_note() -> None:
    event = build_voice_operator_event(
        voice_metadata={"file_id": "voice-file-123"},
        created_by="telegram:42",
        entry_kind="unknown",
    )

    assert event["event_type"] == "operator_voice_note"
    assert event["confidence"] == 0.3
