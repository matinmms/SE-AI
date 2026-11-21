from __future__ import annotations

from typing import Any

from django.conf import settings

from services.ai.providers.base import AICompletionRequest, BaseAIProvider


class ArvanAIProvider(BaseAIProvider):
    """ArvanCloud AI gateway (OpenAI-compatible). Its models, such as Qwen3.5-35B-A3B, run on
    infrastructure inside Iran, so this provider keeps working when the international internet is
    down. The gateway authenticates with "Authorization: apikey <key>", not "Bearer"."""

    provider_name = "arvan"
    provider_key = "arvan"

    def __init__(self, *, requested_model: str, model_override: str | None = None) -> None:
        super().__init__(
            requested_model=requested_model,
            api_key=settings.ARVAN_AI_API_KEY,
            base_url=settings.ARVAN_AI_BASE_URL,
            timeout_seconds=settings.ARVAN_AI_TIMEOUT_SECONDS,
            max_concurrency=settings.ARVAN_AI_PROVIDER_MAX_CONCURRENCY,
            slot_wait_seconds=settings.ARVAN_AI_PROVIDER_SLOT_WAIT_SECONDS,
            slot_poll_seconds=settings.ARVAN_AI_PROVIDER_SLOT_POLL_SECONDS,
        )
        self.model_override = model_override or settings.ARVAN_AI_MODEL

    def authorization_header(self) -> str | None:
        return f"apikey {self.api_key}"

    def resolve_model(self) -> str:
        # Another provider's model name (for example gpt-5.6-luna) means nothing to this gateway.
        return self.model_override or settings.ARVAN_AI_MODEL

    def _prepare_request(self, request: AICompletionRequest) -> AICompletionRequest:
        cap = int(settings.ARVAN_AI_TARGET_MAX_OUTPUT_TOKENS)
        requested = request.max_completion_tokens
        return AICompletionRequest(
            requested_model=request.requested_model,
            system_prompt=str(request.system_prompt or ""),
            user_prompt=str(request.user_prompt or ""),
            temperature=request.temperature,
            max_completion_tokens=cap if requested is None else min(int(requested), cap),
            cost_context=request.cost_context,
            reasoning_effort=None,
        )

    def _build_request_payload(self, *, request: AICompletionRequest, model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": request.messages,
            "temperature": request.temperature,
        }
        if request.max_completion_tokens:
            payload["max_tokens"] = int(request.max_completion_tokens)
        if settings.ARVAN_AI_DISABLE_THINKING:
            # Hidden "thinking" tokens are billed as (expensive) output; notes do not need them.
            payload["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return payload

    def rate_limit_rpm(self) -> int:
        return int(settings.ARVAN_AI_RPM_LIMIT)

    def rate_limit_tpm(self) -> int:
        return int(settings.ARVAN_AI_TPM_LIMIT)

    def rate_limit_wait_seconds(self) -> int:
        return int(settings.ARVAN_AI_RATE_LIMIT_WAIT_SECONDS)

    def rate_limit_poll_seconds(self) -> float:
        return float(settings.ARVAN_AI_RATE_LIMIT_POLL_SECONDS)
