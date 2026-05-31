from pathlib import Path
from types import SimpleNamespace

from operator_journal.voice_transcription import FasterWhisperVoiceTranscriber, NullVoiceTranscriber


def test_null_voice_transcriber_reports_disabled_without_reading_audio() -> None:
    result = NullVoiceTranscriber().transcribe(Path("missing.oga"))

    assert result.success is False
    assert result.provider == "null"
    assert "disabled" in result.error


def test_faster_whisper_prefers_preloaded_model_path(tmp_path: Path) -> None:
    model_dir = tmp_path / "preloaded" / "faster-whisper-medium"
    model_dir.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        (model_dir / name).write_text("x", encoding="utf-8")
    settings = SimpleNamespace(
        voice_transcription_model_cache=str(tmp_path),
        voice_transcription_model="medium",
        voice_transcription_device="cpu",
        voice_transcription_compute_type="int8",
        voice_transcription_language="ru",
    )

    transcriber = FasterWhisperVoiceTranscriber(settings)  # type: ignore[arg-type]

    assert transcriber._model_name_or_path() == str(model_dir)
