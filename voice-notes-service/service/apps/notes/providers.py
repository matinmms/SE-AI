from __future__ import annotations

from collections.abc import Callable
from typing import Any

from django.conf import settings

from apps.ai_pipeline.exceptions import PipelineError
from services.ai.exceptions import AIAllProvidersFailedError
from services.ai.providers.base import (
    AICompletionRequest,
    AICompletionResult,
    REASONING_MODEL_PREFIXES,
)
from services.ai.providers.primary_provider import PrimaryAIProvider
from services.ai.router import AIProviderRouter

LLMResult = AICompletionResult


class GapGPTNoteProvider(AIProviderRouter):
    """Compatibility shim for the legacy note-generation import path."""

    def __init__(self, *, model_name: str) -> None:
        super().__init__(requested_model=model_name)
        self.model = model_name

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        reasoning_effort: str | None = None,
    ) -> AICompletionResult:
        try:
            return super().complete(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
                reasoning_effort=reasoning_effort,
            )
        except AIAllProvidersFailedError as exc:
            raise PipelineError(stage="NOTE_GENERATION", message=exc.message) from exc

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], AICompletionResult]:
        try:
            return super().complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        except AIAllProvidersFailedError as exc:
            raise PipelineError(stage="NOTE_GENERATION", message=exc.message) from exc

    def _build_request_payload(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        is_reasoning_model = self.model.startswith(REASONING_MODEL_PREFIXES)
        normalized_request = AICompletionRequest(
            requested_model=self.model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=None if is_reasoning_model else float(settings.NOTE_LLM_TEMPERATURE),
            max_completion_tokens=(
                None if is_reasoning_model else int(settings.NOTE_LLM_MAX_OUTPUT_TOKENS)
            ),
        )
        provider = self.primary_provider_factory(self.model)
        return provider._build_request_payload(request=normalized_request, model=self.model)

    @staticmethod
    def _resolve_content(payload: Any) -> str:
        return PrimaryAIProvider._resolve_content(payload)

    @staticmethod
    def _sanitize_error_message(message: str) -> str:
        return PrimaryAIProvider._sanitize_error_message(message)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        error = PrimaryAIProvider._classify_exception(exc=exc, model="unknown")
        return bool(error.retryable)
