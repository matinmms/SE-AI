from __future__ import annotations

from abc import ABC, abstractmethod

from services.transcription.models import TranscriptionResult


class BaseTranscriptionProvider(ABC):
    name: str
    model: str

    @abstractmethod
    def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "fa",
        prompt: str | None = None,
        cost_context: dict[str, object] | None = None,
    ) -> TranscriptionResult:
        raise NotImplementedError
