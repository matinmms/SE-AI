from __future__ import annotations

from collections import Counter
from typing import Iterable

from services.ai.providers.base import AICompletionResult


def summarize_completion_results(results: Iterable[AICompletionResult]) -> dict[str, object]:
    materialized = list(results)
    if not materialized:
        return {
            "provider_used": "",
            "model_used": "",
            "fallback_used": False,
            "generation_latency_ms": 0,
            "retry_count": 0,
            "provider_attempt_count": 0,
            "providers_seen": [],
        }

    last = materialized[-1]
    attempts = [attempt for result in materialized for attempt in result.attempts]
    providers_seen = Counter(result.provider for result in materialized)
    return {
        "provider_used": last.provider,
        "model_used": last.model,
        "fallback_used": any(result.fallback_used for result in materialized),
        "generation_latency_ms": sum(int(result.latency_ms or 0) for result in materialized),
        "retry_count": sum(int(result.retry_count or 0) for result in materialized),
        "provider_attempt_count": len(attempts),
        "providers_seen": sorted(providers_seen.keys()),
        "attempts": attempts,
    }

