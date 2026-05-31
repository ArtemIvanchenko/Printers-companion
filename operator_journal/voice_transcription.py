from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from core.config.settings import Settings


@dataclass
class VoiceTranscriptionResult:
    text: str = ""
    provider: str = "null"
    model: str | None = None
    language: str | None = None
    success: bool = False
    error: str | None = None
    segments: list[dict] = field(default_factory=list)


class VoiceTranscriber(Protocol):
    def transcribe(self, audio_path: Path) -> VoiceTranscriptionResult:
        ...


class NullVoiceTranscriber:
    def transcribe(self, audio_path: Path) -> VoiceTranscriptionResult:
        return VoiceTranscriptionResult(error="Voice transcription is disabled.")


class FasterWhisperVoiceTranscriber:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model = None

    def _load_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(
                self._model_name_or_path(),
                device=self.settings.voice_transcription_device,
                compute_type=self.settings.voice_transcription_compute_type,
                download_root=self.settings.voice_transcription_model_cache,
            )
        return self._model

    def _model_name_or_path(self) -> str:
        preloaded_path = (
            Path(self.settings.voice_transcription_model_cache)
            / "preloaded"
            / f"faster-whisper-{self.settings.voice_transcription_model}"
        )
        required_files = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
        if preloaded_path.exists() and all((preloaded_path / name).exists() for name in required_files):
            return str(preloaded_path)
        return self.settings.voice_transcription_model

    def transcribe(self, audio_path: Path) -> VoiceTranscriptionResult:
        try:
            model = self._load_model()
            segments, info = model.transcribe(
                str(audio_path),
                language=self.settings.voice_transcription_language or None,
                vad_filter=True,
                beam_size=5,
            )
            segment_payloads: list[dict] = []
            texts: list[str] = []
            for segment in segments:
                text = segment.text.strip()
                if text:
                    texts.append(text)
                segment_payloads.append(
                    {"start": segment.start, "end": segment.end, "text": segment.text.strip()}
                )
            return VoiceTranscriptionResult(
                text=" ".join(texts).strip(),
                provider="faster_whisper",
                model=self.settings.voice_transcription_model,
                language=getattr(info, "language", self.settings.voice_transcription_language),
                success=True,
                segments=segment_payloads,
            )
        except Exception as exc:
            return VoiceTranscriptionResult(
                provider="faster_whisper",
                model=self.settings.voice_transcription_model,
                language=self.settings.voice_transcription_language,
                error=str(exc),
            )


def get_voice_transcriber(settings: Settings) -> VoiceTranscriber:
    if not settings.voice_transcription_enabled or settings.voice_transcription_provider == "null":
        return NullVoiceTranscriber()
    if settings.voice_transcription_provider == "faster_whisper":
        return FasterWhisperVoiceTranscriber(settings)
    return NullVoiceTranscriber()
