from __future__ import annotations

import uuid

from django.conf import settings

from apps.ai_pipeline.exceptions import PipelineError
from apps.audio_pipeline.providers.base import BaseTranscriptionProvider, TranscriptionPollResult, TranscriptionResult, TranscriptionSubmission
from services.transcription.exceptions import TranscriptionFailedError, TranscriptionValidationError
from services.transcription.router import TranscriptionRouter


class RoutedTranscriptionProvider(BaseTranscriptionProvider):
    transcript_source = settings.TRANSCRIPTION_PRIMARY_PROVIDER

    def __init__(self, router: TranscriptionRouter | None = None) -> None:
        self.router = router or TranscriptionRouter()

    def submit(
        self,
        *,
        file_path: str,
        hotwords: list[str],
        language: str = "fa",
    ) -> TranscriptionSubmission:
        prompt = ", ".join(part.strip() for part in hotwords if part.strip()) or None
        try:
            result = self.router.transcribe(
                audio_path=file_path,
                language=language or settings.AVALAI_STT_LANGUAGE,
                prompt=prompt,
            )
        except TranscriptionValidationError as exc:
            raise PipelineError(stage=exc.stage, message=exc.message) from exc
        except TranscriptionFailedError as exc:
            raise PipelineError(stage=exc.stage, message=exc.message) from exc

        pipeline_result = TranscriptionResult(
            text=result.text,
            raw_text=result.raw_text,
            language=result.language,
            segments=result.segments,
            confidence_score=result.confidence_score,
            raw_response=result.raw_response,
            provider=result.provider,
            model=result.model,
            duration_seconds=result.duration_seconds,
            audio_size_bytes=result.audio_size_bytes,
            chunks_count=result.chunks_count,
            fallback_used=result.fallback_used,
            latency_ms=result.latency_ms,
            metadata=result.metadata,
        )
        return TranscriptionSubmission(
            provider_job_id=f"sync-{uuid.uuid4()}",
            provider_trace_id="",
            status="COMPLETED",
            raw_response={
                "provider": result.provider,
                "model": result.model,
                "fallback_used": result.fallback_used,
                "metadata": result.metadata,
            },
            initial_result=pipeline_result,
        )

    def poll(self, *, provider_job_id: str, provider_trace_id: str) -> TranscriptionPollResult:
        del provider_job_id, provider_trace_id
        raise PipelineError(stage="TRANSCRIBING", message="Routed transcription provider does not support polling.")
