from __future__ import annotations

from pathlib import Path

from apps.audio_pipeline.providers.avanegar import AvanegarTranscriptionProvider
from apps.ai_pipeline.exceptions import PipelineError
from infrastructure.observability.scaling import measure_scaling_stage
from services.transcription.exceptions import TranscriptionProviderError
from services.transcription.models import TranscriptionResult
from services.transcription.providers.base import BaseTranscriptionProvider


class CurrentTranscriptionProvider(BaseTranscriptionProvider):
    name = "current"

    def __init__(self) -> None:
        self.model = "avanegar-default"
        self.provider = AvanegarTranscriptionProvider()

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "fa",
        prompt: str | None = None,
    ) -> TranscriptionResult:
        del language, prompt
        try:
            with measure_scaling_stage(
                stage="transcription_provider_request",
                provider=self.name,
                model=self.model,
                input_size_bytes=Path(audio_path).stat().st_size,
            ) as measurement:
                result = self.provider.transcribe(file_path=audio_path, hotwords=[])
                measurement.add(provider_latency_ms=result.latency_ms)
        except PipelineError as exc:
            raise TranscriptionProviderError(
                message=exc.message,
                provider=self.name,
                model=self.model,
                retryable=True,
                fallback_allowed=False,
            ) from exc

        return TranscriptionResult(
            text=result.text,
            raw_text=result.raw_text,
            provider=self.name,
            model=self.model,
            language=result.language,
            duration_seconds=0.0,
            audio_size_bytes=Path(audio_path).stat().st_size,
            chunks_count=1,
            fallback_used=False,
            latency_ms=0,
            metadata={},
            segments=result.segments,
            confidence_score=result.confidence_score,
            raw_response=result.raw_response,
        )
