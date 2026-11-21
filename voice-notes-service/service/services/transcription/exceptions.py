from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TranscriptionError(Exception):
    message: str
    stage: str = "TRANSCRIBING"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)


@dataclass
class TranscriptionValidationError(TranscriptionError):
    stage: str = "VALIDATION"


@dataclass
class TranscriptionProviderError(TranscriptionError):
    provider: str = ""
    model: str = ""
    retryable: bool = True
    fallback_allowed: bool = True
    status_code: int | None = None


@dataclass
class TranscriptionFailedError(TranscriptionError):
    attempts: list[dict[str, Any]] = field(default_factory=list)
