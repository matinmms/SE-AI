from __future__ import annotations

from typing import Any

from django.conf import settings

from apps.ai_pipeline.exceptions import PipelineError
from apps.audio_pipeline.providers.base import (
    BaseTranscriptionProvider,
    TranscriptionPollResult,
    TranscriptionSubmission,
)


class GapGPTWhisperProvider(BaseTranscriptionProvider):
    def __init__(self) -> None:
        if not settings.GAPGPT_API_KEY:
            raise PipelineError(stage="TRANSCRIBING", message="GAPGPT_API_KEY is not configured.")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on local environment
            raise PipelineError(stage="TRANSCRIBING", message="The openai package is not installed.") from exc

        self.client = OpenAI(
            base_url="https://api.gapgpt.app/v1",
            api_key=settings.GAPGPT_API_KEY,
            timeout=settings.GAPGPT_TRANSCRIPTION_TIMEOUT_SECONDS,
        )

    def _to_dict(self, payload: Any) -> dict[str, object]:
        if hasattr(payload, "model_dump"):
            return payload.model_dump()
        if isinstance(payload, dict):
            return payload
        result: dict[str, object] = {}
        for name in dir(payload):
            if name.startswith("_"):
                continue
            value = getattr(payload, name)
            if callable(value):
                continue
            result[name] = value
        return result

    def _resolve_text(self, payload: Any) -> str:
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            for key in ("text", "transcript", "content"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for key in ("data", "result", "output"):
                nested = payload.get(key)
                if nested:
                    nested_text = self._resolve_text(nested)
                    if nested_text:
                        return nested_text
            segments = payload.get("segments")
            if isinstance(segments, list):
                parts = [
                    str(segment.get("text", "")).strip()
                    for segment in segments
                    if isinstance(segment, dict) and str(segment.get("text", "")).strip()
                ]
                if parts:
                    return " ".join(parts).strip()
        return ""

    def submit(
        self,
        *,
        file_path: str,
        hotwords: list[str],
        language: str = "fa",
    ) -> TranscriptionSubmission:
        try:
            with open(file_path, "rb") as file_handle:
                response = self.client.audio.transcriptions.create(
                    model="gapgpt/whisper-1",
                    file=file_handle,
                    response_format="json",
                    language=language or "fa",
                    prompt="Academic university lecture."
                    + (f" Hotwords and course terms: {', '.join(hotwords)}" if hotwords else ""),
                )
        except Exception as exc:  # pragma: no cover - depends on network/provider
            raise PipelineError(stage="TRANSCRIBING", message=f"Transcription provider request failed: {exc}") from exc

        payload = self._to_dict(response)
        text = self._resolve_text(payload)
        if not text and isinstance(response, str):
            text = response.strip()
            payload = {"text": text}
        segments = payload.get("segments", [])
        if not isinstance(segments, list):
            segments = []

        confidence_score = None
        confidences = [
            float(segment.get("avg_logprob"))
            for segment in segments
            if isinstance(segment, dict) and segment.get("avg_logprob") is not None
        ]
        if confidences:
            confidence_score = round(sum(confidences) / len(confidences), 4)

        self._last_result = TranscriptionPollResult(
            status="COMPLETED",
            text=text,
            raw_text=text,
            language=str(payload.get("language", "") or ""),
            segments=segments,
            confidence_score=confidence_score,
            raw_response=payload,
            processing_metadata={},
        )
        return TranscriptionSubmission(
            provider_job_id=f"sync-{abs(hash(file_path))}",
            provider_trace_id="sync-gapgpt",
            status="COMPLETED",
            raw_response=payload,
        )

    def poll(self, *, provider_job_id: str, provider_trace_id: str) -> TranscriptionPollResult:
        if not hasattr(self, "_last_result"):
            raise PipelineError(stage="TRANSCRIBING", message="GapGPT provider has no cached synchronous result to poll.")
        return self._last_result
