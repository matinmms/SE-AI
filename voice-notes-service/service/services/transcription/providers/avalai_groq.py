from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from django.conf import settings

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI, OpenAIError, RateLimitError

from apps.audio_pipeline.transcript_text import unwrap_transcript_text
from infrastructure.observability.scaling import measure_scaling_stage
from infrastructure.queue.control import provider_concurrency_slot
from services.transcription.exceptions import TranscriptionProviderError
from services.transcription.models import TranscriptionResult
from services.transcription.providers.base import BaseTranscriptionProvider

logger = logging.getLogger("tesanote")


class AvalAIGroqWhisperProvider(BaseTranscriptionProvider):
    name = "avalai_groq_whisper"

    def __init__(self) -> None:
        self.model = settings.AVALAI_STT_MODEL
        if not settings.AVALAI_API_KEY:
            raise TranscriptionProviderError(
                message="AVALAI_API_KEY is not configured.",
                provider=self.name,
                model=self.model,
                retryable=False,
                fallback_allowed=False,
            )
        self.client = OpenAI(
            api_key=settings.AVALAI_API_KEY,
            base_url=settings.AVALAI_BASE_URL,
            timeout=settings.AVALAI_STT_TIMEOUT_SECONDS,
        )

    def transcribe(
        self,
        audio_path: str,
        *,
        language: str = "fa",
        prompt: str | None = None,
        cost_context: dict[str, object] | None = None,
    ) -> TranscriptionResult:
        started_at = time.perf_counter()
        file_size = Path(audio_path).stat().st_size
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "response_format": settings.AVALAI_STT_RESPONSE_FORMAT,
            "language": language or settings.AVALAI_STT_LANGUAGE,
            "temperature": settings.AVALAI_STT_TEMPERATURE,
        }
        if prompt:
            request_kwargs["prompt"] = prompt
        from services.note_review.eligibility import capture_wanted_for_current_call

        capture_segments = capture_wanted_for_current_call(cost_context)
        if capture_segments:
            # Same transcript text as the plain format; adds per-segment times and recognizer stats.
            request_kwargs["response_format"] = "verbose_json"
            request_kwargs["timestamp_granularities"] = ["segment"]

        logger.info(
            "avalai_transcription_started",
            extra={
                "provider": self.name,
                "model": self.model,
                "audio_size_bytes": file_size,
            },
        )

        try:
            with measure_scaling_stage(
                stage="transcription_provider_request",
                provider=self.name,
                model=self.model,
                input_size_bytes=file_size,
            ) as measurement:
                with open(audio_path, "rb") as audio_file:
                    with provider_concurrency_slot(
                        provider_key="avalai_stt",
                        label="AvalAI STT provider",
                        max_concurrency=settings.AVALAI_PROVIDER_MAX_CONCURRENCY,
                        wait_timeout_seconds=settings.AVALAI_PROVIDER_SLOT_WAIT_SECONDS,
                        poll_interval_seconds=settings.AVALAI_PROVIDER_SLOT_POLL_SECONDS,
                        lease_seconds=max(30, settings.AVALAI_STT_TIMEOUT_SECONDS + 15),
                        stage="TRANSCRIBING",
                    ):
                        try:
                            raw_response = self.client.audio.transcriptions.with_raw_response.create(
                                file=audio_file,
                                **request_kwargs,
                            )
                        except APIStatusError as exc:
                            if not capture_segments or exc.status_code not in {400, 422}:
                                raise
                            # The route refused the verbose format: fall back to the plain one so the
                            # lecture still gets its transcript; the review just has no segments.
                            capture_segments = False
                            request_kwargs["response_format"] = settings.AVALAI_STT_RESPONSE_FORMAT
                            request_kwargs.pop("timestamp_granularities", None)
                            audio_file.seek(0)
                            raw_response = self.client.audio.transcriptions.with_raw_response.create(
                                file=audio_file,
                                **request_kwargs,
                            )
                        response = raw_response.parse()
                measurement.add(provider_latency_ms=round((time.perf_counter() - started_at) * 1000))
        except (APITimeoutError, APIConnectionError) as exc:
            raise TranscriptionProviderError(
                message=f"AvalAI transcription request timed out or could not connect: {exc}",
                provider=self.name,
                model=self.model,
                retryable=True,
                fallback_allowed=True,
            ) from exc
        except RateLimitError as exc:
            raise TranscriptionProviderError(
                message="AvalAI rate limited the transcription request.",
                provider=self.name,
                model=self.model,
                retryable=True,
                fallback_allowed=True,
                status_code=429,
            ) from exc
        except APIStatusError as exc:
            retryable = exc.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
            raise TranscriptionProviderError(
                message=f"AvalAI returned HTTP {exc.status_code} for transcription.",
                provider=self.name,
                model=self.model,
                retryable=retryable,
                fallback_allowed=retryable,
                status_code=exc.status_code,
            ) from exc
        except OpenAIError as exc:
            raise TranscriptionProviderError(
                message=f"AvalAI transcription failed: {exc}",
                provider=self.name,
                model=self.model,
                retryable=True,
                fallback_allowed=True,
            ) from exc
        except OSError as exc:
            raise TranscriptionProviderError(
                message=f"Unable to read audio file for AvalAI transcription: {exc}",
                provider=self.name,
                model=self.model,
                retryable=False,
                fallback_allowed=False,
            ) from exc

        text = response if isinstance(response, str) else str(getattr(response, "text", "") or "").strip()
        text = unwrap_transcript_text(text).strip()
        if prompt:
            from apps.audio_pipeline.hotwords.stt_prompt import strip_prompt_echo

            text = strip_prompt_echo(text, prompt)
        segments = self._extract_segments(response) if capture_segments else []
        latency_ms = int((time.perf_counter() - started_at) * 1000)
        if not text:
            raise TranscriptionProviderError(
                message="AvalAI returned an empty transcript.",
                provider=self.name,
                model=self.model,
                retryable=True,
                fallback_allowed=True,
            )

        from apps.ai_costs.services import extract_avalai_request_id, record_avalai_request

        request_id, request_id_source = extract_avalai_request_id(getattr(raw_response, "headers", None))
        if request_id:
            record_avalai_request(
                request_id=request_id,
                request_id_source=request_id_source,
                operation="transcription",
                feature="transcription",
                requested_model=self.model,
                context=cost_context,
            )

        logger.info(
            "avalai_transcription_success",
            extra={
                "provider": self.name,
                "model": self.model,
                "audio_size_bytes": file_size,
                "latency_ms": latency_ms,
            },
        )

        return TranscriptionResult(
            text=text,
            raw_text=text,
            provider=self.name,
            model=self.model,
            language=language or settings.AVALAI_STT_LANGUAGE,
            duration_seconds=0.0,
            audio_size_bytes=file_size,
            chunks_count=1,
            fallback_used=False,
            latency_ms=latency_ms,
            metadata={
                "response_format": request_kwargs["response_format"],
                "temperature": settings.AVALAI_STT_TEMPERATURE,
            },
            segments=segments,
            raw_response={},
        )

    @staticmethod
    def _extract_segments(response: Any) -> list[dict[str, Any]]:
        """Normalize verbose_json segments; anything unexpected yields no segments, never an error."""
        try:
            raw_segments = response.get("segments") if isinstance(response, dict) else getattr(response, "segments", None)
            segments: list[dict[str, Any]] = []
            for raw in raw_segments or []:
                data = raw if isinstance(raw, dict) else getattr(raw, "model_dump", lambda: {})()
                if "start" not in data or "end" not in data:
                    continue
                segments.append(
                    {
                        "start": float(data["start"]),
                        "end": float(data["end"]),
                        "text": str(data.get("text") or ""),
                        "avg_logprob": data.get("avg_logprob"),
                        "no_speech_prob": data.get("no_speech_prob"),
                        "compression_ratio": data.get("compression_ratio"),
                        "seek": data.get("seek", 0),
                    }
                )
            return segments
        except Exception:
            logger.warning("avalai_segment_parse_failed")
            return []
