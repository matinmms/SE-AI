from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranscriptionResult:
    text: str
    provider: str
    model: str
    language: str
    duration_seconds: float
    audio_size_bytes: int
    chunks_count: int
    fallback_used: bool
    latency_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    confidence_score: float | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.raw_text:
            self.raw_text = self.text

