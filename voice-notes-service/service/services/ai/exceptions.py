from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass
class AIProviderError(Exception):
    provider: str
    model: str
    message: str
    retryable: bool = False
    status_code: int | None = None
    category: str = "provider_error"

    CATEGORY: ClassVar[str] = "provider_error"

    def __post_init__(self) -> None:
        self.category = getattr(type(self), "CATEGORY", self.category)

    def __str__(self) -> str:
        return self.message


class AIProviderTimeoutError(AIProviderError):
    CATEGORY = "timeout"


class AIProviderRateLimitError(AIProviderError):
    CATEGORY = "rate_limit"


class AIProviderServerError(AIProviderError):
    CATEGORY = "server_error"


class AIProviderUnavailableError(AIProviderError):
    CATEGORY = "unavailable"


class AIProviderMalformedResponseError(AIProviderError):
    CATEGORY = "malformed_response"


class AIProviderAuthenticationError(AIProviderError):
    CATEGORY = "authentication"


class AIAllProvidersFailedError(Exception):
    def __init__(self, *, message: str, attempts: list[dict[str, object]]) -> None:
        super().__init__(message)
        self.message = message
        self.attempts = attempts
