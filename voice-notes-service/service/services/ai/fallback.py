from __future__ import annotations

from dataclasses import dataclass

from services.ai.exceptions import AIProviderError


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int
    base_delay_seconds: float

    def should_retry(self, *, error: AIProviderError, attempt_index: int) -> bool:
        return error.retryable and attempt_index < self.max_retries

    def backoff_seconds(self, *, attempt_index: int) -> float:
        return self.base_delay_seconds * max(1, attempt_index + 1)


@dataclass(frozen=True)
class FallbackPolicy:
    primary_retries: RetryPolicy
    fallback_retries: RetryPolicy

    @staticmethod
    def should_fallback(*, error: AIProviderError) -> bool:
        return error.retryable or error.category in {"malformed_response", "provider_error", "rate_limit"}
