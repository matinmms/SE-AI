from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class TranscriptionResult:
    text: str
    raw_text: str
    language: str
    segments: list[dict[str, object]]
    confidence_score: float | None
    raw_response: dict[str, object]
    provider: str = ""
    model: str = ""
    duration_seconds: float = 0.0
    audio_size_bytes: int = 0
    chunks_count: int = 1
    fallback_used: bool = False
    latency_ms: int = 0
    metadata: dict[str, Any] | None = None


@dataclass
class TranscriptionSubmission:
    provider_job_id: str
    provider_trace_id: str
    status: str
    raw_response: dict[str, object]
    initial_result: TranscriptionResult | None = None


@dataclass
class TranscriptionPollResult:
    status: str
    text: str
    raw_text: str
    language: str
    segments: list[dict[str, object]]
    confidence_score: float | None
    raw_response: dict[str, object]
    processing_metadata: dict[str, object]


class BaseTranscriptionProvider(ABC):
    @abstractmethod
    def submit(
        self,
        *,
        file_path: str,
        hotwords: list[str],
        language: str = "fa",
    ) -> TranscriptionSubmission:
        raise NotImplementedError

    @abstractmethod
    def poll(self, *, provider_job_id: str, provider_trace_id: str) -> TranscriptionPollResult:
        raise NotImplementedError

    def transcribe(
        self,
        *,
        file_path: str,
        hotwords: list[str],
        language: str = "fa",
    ) -> TranscriptionResult:
        submission = self.submit(file_path=file_path, hotwords=hotwords, language=language)
        if submission.initial_result is not None:
            return submission.initial_result
        result = self.poll(
            provider_job_id=submission.provider_job_id,
            provider_trace_id=submission.provider_trace_id,
        )
        return TranscriptionResult(
            text=result.text,
            raw_text=result.raw_text,
            language=result.language,
            segments=result.segments,
            confidence_score=result.confidence_score,
            raw_response=result.raw_response,
            metadata=result.processing_metadata,
        )
