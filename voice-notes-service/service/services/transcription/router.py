from __future__ import annotations

import logging
import inspect
import time
from pathlib import Path
from typing import Any

from django.conf import settings

from services.transcription.chunking import PreparedTranscriptionPart, TranscriptionChunkPreparer
from services.transcription.exceptions import (
    TranscriptionFailedError,
    TranscriptionProviderError,
    TranscriptionValidationError,
)
from services.transcription.models import TranscriptionResult
from services.transcription.providers import AvalAIGroqWhisperProvider, CurrentTranscriptionProvider
from services.transcription.providers.base import BaseTranscriptionProvider

logger = logging.getLogger("tesanote")


class TranscriptionRouter:
    def __init__(
        self,
        *,
        primary_provider: BaseTranscriptionProvider | None = None,
        fallback_provider: BaseTranscriptionProvider | None = None,
        chunk_preparer: TranscriptionChunkPreparer | None = None,
    ) -> None:
        self._primary_provider = primary_provider
        self._fallback_provider = fallback_provider
        self.primary_provider_key = settings.TRANSCRIPTION_PRIMARY_PROVIDER
        self.fallback_provider_key = settings.TRANSCRIPTION_FALLBACK_PROVIDER
        self.chunk_preparer = chunk_preparer or TranscriptionChunkPreparer()

    @property
    def primary_provider(self) -> BaseTranscriptionProvider:
        if self._primary_provider is None:
            self._primary_provider = self._build_provider(self.primary_provider_key)
        return self._primary_provider

    @property
    def fallback_provider(self) -> BaseTranscriptionProvider:
        if self._fallback_provider is None:
            self._fallback_provider = self._build_provider(self.fallback_provider_key)
        return self._fallback_provider

    @staticmethod
    def _build_provider(provider_key: str) -> BaseTranscriptionProvider:
        normalized = str(provider_key or "").strip().lower()
        if normalized == "avalai_groq_whisper":
            return AvalAIGroqWhisperProvider()
        if normalized == "current":
            return CurrentTranscriptionProvider()
        raise TranscriptionValidationError(message=f"Unknown transcription provider: {provider_key}")

    def transcribe(
        self,
        *,
        audio_path: str,
        language: str | None = None,
        prompt: str | None = None,
        lecture_id: str | None = None,
        user_id: str | None = None,
    ) -> TranscriptionResult:
        attempts: list[dict[str, Any]] = []
        language_code = language or settings.AVALAI_STT_LANGUAGE

        logger.info(
            "transcription_started",
            extra={
                "lecture_id": lecture_id or "",
                "user_id": user_id or "",
                "primary_provider": self.primary_provider.name,
                "fallback_provider": self.fallback_provider.name,
            },
        )

        with self.chunk_preparer.prepare(audio_path=audio_path) as prepared_parts:
            try:
                logger.info(
                    "transcription_primary_provider_selected",
                    extra={
                        "lecture_id": lecture_id or "",
                        "user_id": user_id or "",
                        "provider": self.primary_provider.name,
                        "model": self.primary_provider.model,
                        "chunk_count": len(prepared_parts),
                    },
                )
                return self._transcribe_parts(
                    provider=self.primary_provider,
                    prepared_parts=prepared_parts,
                    language=language_code,
                    prompt=prompt,
                    attempts=attempts,
                    lecture_id=lecture_id,
                    user_id=user_id,
                    fallback_used=False,
                )
            except TranscriptionProviderError as primary_error:
                attempts.append(
                    {
                        "provider": primary_error.provider,
                        "model": primary_error.model,
                        "error_message": primary_error.message,
                        "status_code": primary_error.status_code,
                    }
                )
                logger.warning(
                    "avalai_transcription_failed",
                    extra={
                        "lecture_id": lecture_id or "",
                        "user_id": user_id or "",
                        "provider": primary_error.provider,
                        "model": primary_error.model,
                        "error_message": primary_error.message,
                        "status_code": primary_error.status_code,
                    },
                )
                if not primary_error.fallback_allowed:
                    raise TranscriptionFailedError(message=primary_error.message, attempts=attempts) from primary_error

            logger.warning(
                "transcription_fallback_started",
                extra={
                    "lecture_id": lecture_id or "",
                    "user_id": user_id or "",
                    "provider": self.fallback_provider.name,
                    "model": self.fallback_provider.model,
                    "chunk_count": len(prepared_parts),
                },
            )
            try:
                return self._transcribe_parts(
                    provider=self.fallback_provider,
                    prepared_parts=prepared_parts,
                    language=language_code,
                    prompt=prompt,
                    attempts=attempts,
                    lecture_id=lecture_id,
                    user_id=user_id,
                    fallback_used=True,
                )
            except TranscriptionProviderError as fallback_error:
                attempts.append(
                    {
                        "provider": fallback_error.provider,
                        "model": fallback_error.model,
                        "error_message": fallback_error.message,
                        "status_code": fallback_error.status_code,
                    }
                )
                logger.error(
                    "transcription_all_providers_failed",
                    extra={
                        "lecture_id": lecture_id or "",
                        "user_id": user_id or "",
                        "attempts": attempts,
                    },
                )
                raise TranscriptionFailedError(
                    message="پردازش صوت با مشکل مواجه شد. لطفا کمی بعد دوباره تلاش کنید.",
                    attempts=attempts,
                    metadata={"audio_path": audio_path},
                ) from fallback_error

    def _transcribe_parts(
        self,
        *,
        provider: BaseTranscriptionProvider,
        prepared_parts: list[PreparedTranscriptionPart],
        language: str,
        prompt: str | None,
        attempts: list[dict[str, Any]],
        lecture_id: str | None,
        user_id: str | None,
        fallback_used: bool,
    ) -> TranscriptionResult:
        combined_texts: list[str] = []
        combined_segments: list[dict[str, Any]] = []
        total_latency_ms = 0
        total_audio_size_bytes = 0
        prompt_tail = prompt or ""

        for index, part in enumerate(prepared_parts):
            logger.info(
                "chunk_transcription_started",
                extra={
                    "lecture_id": lecture_id or "",
                    "user_id": user_id or "",
                    "provider": provider.name,
                    "model": provider.model,
                    "chunk_index": index,
                    "chunk_count": len(prepared_parts),
                    "duration_seconds": round(part.duration_seconds, 3),
                },
            )
            result = self._transcribe_part_with_retries(
                provider=provider,
                part=part,
                language=language,
                prompt=prompt_tail or None,
                lecture_id=lecture_id,
                user_id=user_id,
            )
            if not result.text.strip():
                raise TranscriptionProviderError(
                    message="Provider returned an empty transcript chunk.",
                    provider=provider.name,
                    model=provider.model,
                    retryable=True,
                    fallback_allowed=provider is self.primary_provider,
                )
            combined_texts.append(result.text.strip())
            for segment in result.segments or []:
                if not isinstance(segment, dict) or "start" not in segment:
                    combined_segments.append(segment)
                    continue
                # Segment times are relative to this part; add the part's offset inside the file
                # that was routed so callers get times on one timeline per routed file.
                shifted = dict(segment)
                shifted["start"] = float(segment["start"]) + float(part.start_ts)
                shifted["end"] = float(segment["end"]) + float(part.start_ts)
                shifted["window"] = f"{index}:{segment.get('seek', 0)}"
                combined_segments.append(shifted)
            total_latency_ms += result.latency_ms
            total_audio_size_bytes += result.audio_size_bytes
            prompt_tail = result.text[-400:]
            logger.info(
                "chunk_transcription_success",
                extra={
                    "lecture_id": lecture_id or "",
                    "user_id": user_id or "",
                    "provider": provider.name,
                    "model": provider.model,
                    "chunk_index": index,
                    "chunk_count": len(prepared_parts),
                    "latency_ms": result.latency_ms,
                },
            )

        text = "\n".join(part for part in combined_texts if part).strip()
        if not text:
            raise TranscriptionProviderError(
                message="Provider returned an empty transcript.",
                provider=provider.name,
                model=provider.model,
                retryable=True,
                fallback_allowed=provider is self.primary_provider,
            )

        metadata = {
            "attempts": attempts,
            "provider": provider.name,
            "model": provider.model,
            "prepared_chunk_count": len(prepared_parts),
            "prepared_chunks": [
                {
                    "chunk_index": part.chunk_index,
                    "start_ts": part.start_ts,
                    "end_ts": part.end_ts,
                    "duration_seconds": round(part.duration_seconds, 3),
                    "audio_size_bytes": part.audio_size_bytes,
                }
                for part in prepared_parts
            ],
        }
        if fallback_used:
            logger.info(
                "fallback_transcription_success",
                extra={
                    "lecture_id": lecture_id or "",
                    "user_id": user_id or "",
                    "provider": provider.name,
                    "model": provider.model,
                    "chunk_count": len(prepared_parts),
                },
            )

        return TranscriptionResult(
            text=text,
            raw_text=text,
            provider=provider.name,
            model=provider.model,
            language=language,
            duration_seconds=round(sum(part.duration_seconds for part in prepared_parts), 3),
            audio_size_bytes=total_audio_size_bytes,
            chunks_count=len(prepared_parts),
            fallback_used=fallback_used,
            latency_ms=total_latency_ms,
            metadata=metadata,
            segments=combined_segments,
            raw_response={},
        )

    def _transcribe_part_with_retries(
        self,
        *,
        provider: BaseTranscriptionProvider,
        part: PreparedTranscriptionPart,
        language: str,
        prompt: str | None,
        lecture_id: str | None = None,
        user_id: str | None = None,
    ) -> TranscriptionResult:
        max_attempts = max(
            1,
            settings.AVALAI_STT_MAX_RETRIES if provider is self.primary_provider else settings.AVANEGAR_REQUEST_MAX_RETRIES,
        )
        last_error: TranscriptionProviderError | None = None

        for attempt in range(1, max_attempts + 1):
            started_at = time.perf_counter()
            try:
                if provider.name == "avalai_groq_whisper" and "cost_context" in inspect.signature(provider.transcribe).parameters:
                    # A dict with empty-string values is still truthy, which would
                    # make `record_avalai_request` skip its `current_cost_context()`
                    # fallback and persist a cost row with no lecture/user linkage.
                    # Only pass an explicit context when we actually have identifiers.
                    explicit_cost_context = (
                        {"lecture_id": lecture_id, "user_id": user_id}
                        if (lecture_id or user_id)
                        else None
                    )
                    result = provider.transcribe(
                        part.path,
                        language=language,
                        prompt=prompt,
                        cost_context=explicit_cost_context,
                    )
                else:
                    result = provider.transcribe(part.path, language=language, prompt=prompt)
                result.duration_seconds = round(part.duration_seconds, 3)
                result.audio_size_bytes = part.audio_size_bytes or Path(part.path).stat().st_size
                result.chunks_count = 1
                if result.latency_ms <= 0:
                    result.latency_ms = int((time.perf_counter() - started_at) * 1000)
                result.metadata = {
                    **(result.metadata or {}),
                    "chunk_index": part.chunk_index,
                    "start_ts": part.start_ts,
                    "end_ts": part.end_ts,
                    "attempt": attempt,
                }
                return result
            except TranscriptionProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt >= max_attempts:
                    break

        assert last_error is not None
        raise last_error
