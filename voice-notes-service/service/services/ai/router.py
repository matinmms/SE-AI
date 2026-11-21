from __future__ import annotations

import logging
import time
from contextvars import copy_context
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from django.conf import settings
from django.db import close_old_connections

from apps.admin_ops.system_mode import is_emergency
from apps.ai_pipeline.exceptions import PipelineError
from services.ai.exceptions import (
    AIAllProvidersFailedError,
    AIProviderError,
    AIProviderTimeoutError,
)
from services.ai.fallback import FallbackPolicy, RetryPolicy
from services.ai.providers import (
    AICompletionRequest,
    AICompletionResult,
    ArvanAIProvider,
    AvalAIProvider,
    BaseAIProvider,
    PrimaryAIProvider,
)
from services.ai.providers.base import REASONING_MODEL_PREFIXES

logger = logging.getLogger("tesanote.ai")


class AIProviderRouter:
    def __init__(
        self,
        *,
        requested_model: str,
        primary_provider_factory: Callable[[str], BaseAIProvider] | None = None,
        fallback_provider_factory: Callable[[str], BaseAIProvider] | None = None,
        fallback_policy: FallbackPolicy | None = None,
    ) -> None:
        self.requested_model = requested_model
        self.primary_provider_key = str(settings.NOTE_PRIMARY_PROVIDER or "avalai").strip().lower()
        self.fallback_provider_key = str(settings.NOTE_FALLBACK_PROVIDER or "gapgpt").strip().lower()
        self.fallback_model = str(settings.NOTE_FALLBACK_MODEL or "gemini-2.5-flash-lite").strip()
        if is_emergency():
            # International internet is down: only Iran-hosted models are reachable.
            self.requested_model = settings.EMERGENCY_AI_PRIMARY_MODEL or requested_model
            self.primary_provider_key = settings.EMERGENCY_AI_PRIMARY_PROVIDER or self.primary_provider_key
            self.fallback_provider_key = settings.EMERGENCY_AI_FALLBACK_PROVIDER or self.fallback_provider_key
            self.fallback_model = settings.EMERGENCY_AI_FALLBACK_MODEL or self.fallback_model
        self.primary_provider_factory = primary_provider_factory or (
            lambda model: self._build_provider(provider_key=self.primary_provider_key, model=model)
        )
        self.fallback_provider_factory = fallback_provider_factory or (
            lambda _model: self._build_provider(provider_key=self.fallback_provider_key, model=self.fallback_model)
        )
        self._fallback_until = 0.0
        self.fallback_policy = fallback_policy or FallbackPolicy(
            primary_retries=self._retry_policy_for(provider_key=self.primary_provider_key),
            fallback_retries=self._retry_policy_for(provider_key=self.fallback_provider_key),
        )

    # After the primary fails, stay on the fallback only briefly. The fallback model is
    # weaker; pinning it for the rest of a long job turned one transient primary error
    # into invalid-JSON failures on every later call.
    PRIMARY_RETRY_COOLDOWN_SECONDS = 60.0

    def _fallback_active(self) -> bool:
        return time.monotonic() < self._fallback_until

    @staticmethod
    def _build_provider(*, provider_key: str, model: str) -> BaseAIProvider:
        if provider_key == "avalai":
            return AvalAIProvider(requested_model=model, model_override=model)
        if provider_key == "gapgpt":
            return PrimaryAIProvider(requested_model=model)
        if provider_key == "arvan":
            return ArvanAIProvider(requested_model=model, model_override=model)
        raise PipelineError(stage="NOTE_GENERATION", message=f"Unknown note-generation provider: {provider_key}")

    @staticmethod
    def _retry_policy_for(*, provider_key: str) -> RetryPolicy:
        if provider_key == "avalai":
            return RetryPolicy(
                max_retries=settings.AVALAI_MAX_RETRIES,
                base_delay_seconds=settings.AVALAI_RETRY_BASE_DELAY_SECONDS,
            )
        if provider_key == "gapgpt":
            return RetryPolicy(
                max_retries=settings.PRIMARY_AI_MAX_RETRIES,
                base_delay_seconds=settings.PRIMARY_AI_RETRY_BASE_DELAY_SECONDS,
            )
        if provider_key == "arvan":
            return RetryPolicy(
                max_retries=settings.ARVAN_AI_MAX_RETRIES,
                base_delay_seconds=settings.ARVAN_AI_RETRY_BASE_DELAY_SECONDS,
            )
        return RetryPolicy(max_retries=0, base_delay_seconds=0)

    def complete(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        reasoning_effort: str | None = None,
    ) -> AICompletionResult:
        request = self._build_request(system_prompt=system_prompt, user_prompt=user_prompt)
        request.reasoning_effort = reasoning_effort or None
        attempts: list[dict[str, object]] = []
        if not self._fallback_active():
            try:
                primary_provider = self.primary_provider_factory(self.requested_model)
            except PipelineError as exc:
                raise AIAllProvidersFailedError(
                    message=str(exc),
                    attempts=[{"provider": self.primary_provider_key, "model": self.requested_model, "outcome": "failed", "error_message": str(exc)}],
                ) from exc

            try:
                result = self._execute_with_retry(
                    provider=primary_provider,
                    request=request,
                    retry_policy=self.fallback_policy.primary_retries,
                    event_callback=event_callback,
                )
                result.attempts = attempts + result.attempts
                return result
            except AIProviderError as primary_error:
                attempts.extend(
                    getattr(primary_error, "attempts", [])
                    if hasattr(primary_error, "attempts")
                    else []
                )
                logger.warning(
                    "ai_primary_provider_failed",
                    extra={
                        "provider": primary_error.provider,
                        "model": primary_error.model,
                        "category": primary_error.category,
                        "status_code": primary_error.status_code,
                        "error_message": primary_error.message,
                    },
                )
                if not self.fallback_policy.should_fallback(error=primary_error):
                    raise
                self._fallback_until = time.monotonic() + self.PRIMARY_RETRY_COOLDOWN_SECONDS
                self._emit_event(
                    event_callback,
                    {
                        "event": "provider_fallback_triggered",
                        "from_provider": primary_error.provider,
                        "from_model": primary_error.model,
                        "to_provider": self.fallback_provider_key,
                        "to_model": self.fallback_model,
                        "message": primary_error.message,
                    },
                )

        try:
            fallback_provider = self.fallback_provider_factory(self.requested_model)
        except PipelineError as exc:
            attempts.append({"provider": self.fallback_provider_key, "model": self.fallback_model, "outcome": "failed", "error_message": str(exc)})
            raise AIAllProvidersFailedError(
                message=self._format_final_error(attempts=attempts),
                attempts=attempts,
            ) from exc
        try:
            result = self._execute_with_retry(
                provider=fallback_provider,
                request=request,
                retry_policy=self.fallback_policy.fallback_retries,
                event_callback=event_callback,
                fallback_used=True,
            )
            self._fallback_until = time.monotonic() + self.PRIMARY_RETRY_COOLDOWN_SECONDS
            result.attempts = attempts + result.attempts
            return result
        except AIProviderError as fallback_error:
            attempts.extend(
                getattr(fallback_error, "attempts", [])
                if hasattr(fallback_error, "attempts")
                else []
            )
            logger.error(
                "ai_all_providers_failed",
                extra={
                    "requested_model": self.requested_model,
                    "fallback_model": self.fallback_model,
                    "attempts": attempts,
                    "final_provider": fallback_error.provider,
                    "final_message": fallback_error.message,
                },
            )
            raise AIAllProvidersFailedError(
                message=self._format_final_error(attempts=attempts),
                attempts=attempts,
            ) from fallback_error

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> tuple[dict[str, Any], AICompletionResult]:
        request = self._build_request(system_prompt=system_prompt, user_prompt=user_prompt)
        attempts: list[dict[str, object]] = []
        if not self._fallback_active():
            try:
                primary_provider = self.primary_provider_factory(self.requested_model)
            except PipelineError as exc:
                raise AIAllProvidersFailedError(
                    message=str(exc),
                    attempts=[{"provider": self.primary_provider_key, "model": self.requested_model, "outcome": "failed", "error_message": str(exc)}],
                ) from exc
            try:
                return self._execute_json_with_retry(
                    provider=primary_provider,
                    request=request,
                    retry_policy=self.fallback_policy.primary_retries,
                    event_callback=event_callback,
                    attempts_prefix=attempts,
                    fallback_used=False,
                )
            except AIProviderError as primary_error:
                attempts.extend(getattr(primary_error, "attempts", []) if hasattr(primary_error, "attempts") else [])
                if not self.fallback_policy.should_fallback(error=primary_error):
                    raise
                self._fallback_until = time.monotonic() + self.PRIMARY_RETRY_COOLDOWN_SECONDS
                self._emit_event(
                    event_callback,
                    {
                        "event": "provider_fallback_triggered",
                        "from_provider": primary_error.provider,
                        "from_model": primary_error.model,
                        "to_provider": self.fallback_provider_key,
                        "to_model": self.fallback_model,
                        "message": primary_error.message,
                    },
                )

        try:
            fallback_provider = self.fallback_provider_factory(self.requested_model)
        except PipelineError as exc:
            attempts.append({"provider": self.fallback_provider_key, "model": self.fallback_model, "outcome": "failed", "error_message": str(exc)})
            raise AIAllProvidersFailedError(
                message=self._format_final_error(attempts=attempts),
                attempts=attempts,
            ) from exc
        try:
            payload, result = self._execute_json_with_retry(
                provider=fallback_provider,
                request=request,
                retry_policy=self.fallback_policy.fallback_retries,
                event_callback=event_callback,
                attempts_prefix=attempts,
                fallback_used=True,
            )
            self._fallback_until = time.monotonic() + self.PRIMARY_RETRY_COOLDOWN_SECONDS
            return payload, result
        except AIProviderError as fallback_error:
            attempts.extend(getattr(fallback_error, "attempts", []) if hasattr(fallback_error, "attempts") else [])
            raise AIAllProvidersFailedError(
                message=self._format_final_error(attempts=attempts),
                attempts=attempts,
            ) from fallback_error

    def _execute_with_retry(
        self,
        *,
        provider: BaseAIProvider,
        request: AICompletionRequest,
        retry_policy: RetryPolicy,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        fallback_used: bool = False,
    ) -> AICompletionResult:
        attempts: list[dict[str, object]] = []
        last_error: AIProviderError | None = None

        for attempt_index in range(retry_policy.max_retries + 1):
            self._emit_event(
                event_callback,
                {
                    "event": "provider_attempt",
                    "provider": provider.provider_name,
                    "model": provider.resolve_model(),
                    "attempt": attempt_index + 1,
                    "max_attempts": retry_policy.max_retries + 1,
                },
            )
            try:
                result = self._run_provider_call_with_hard_timeout(
                    provider=provider,
                    fn=lambda: provider.complete(
                        request=request,
                        event_callback=event_callback,
                        attempt_number=attempt_index + 1,
                    ),
                    event_callback=event_callback,
                    attempt_number=attempt_index + 1,
                )
                result.fallback_used = fallback_used
                result.retry_count = attempt_index
                result.attempts = [
                    *attempts,
                    {
                        "provider": result.provider,
                        "model": result.model,
                        "attempt": attempt_index + 1,
                        "latency_ms": result.latency_ms,
                        "outcome": "success",
                        "retryable": False,
                    },
                ]
                logger.info(
                    "ai_provider_route_succeeded",
                    extra={
                        "provider": result.provider,
                        "model": result.model,
                        "fallback_used": fallback_used,
                        "retry_count": attempt_index,
                        "latency_ms": result.latency_ms,
                    },
                )
                return result
            except AIProviderError as error:
                last_error = error
                attempts.append(
                    {
                        "provider": error.provider,
                        "model": error.model,
                        "attempt": attempt_index + 1,
                        "latency_ms": 0,
                        "outcome": "failed",
                        "error_type": error.category,
                        "error_message": error.message,
                        "status_code": error.status_code,
                        "retryable": error.retryable,
                    }
                )
                if not retry_policy.should_retry(error=error, attempt_index=attempt_index):
                    error.attempts = attempts  # type: ignore[attr-defined]
                    raise

                delay_seconds = retry_policy.backoff_seconds(attempt_index=attempt_index)
                logger.warning(
                    "ai_provider_retry_scheduled",
                    extra={
                        "provider": error.provider,
                        "model": error.model,
                        "attempt": attempt_index + 1,
                        "retry_in_seconds": delay_seconds,
                        "category": error.category,
                    },
                )
                self._emit_event(
                    event_callback,
                    {
                        "event": "provider_retry_sleep",
                        "provider": error.provider,
                        "model": error.model,
                        "attempt": attempt_index + 1,
                        "max_attempts": retry_policy.max_retries + 1,
                        "retry_delay_seconds": delay_seconds,
                        "message": error.message,
                    },
                )
                time.sleep(delay_seconds)

        assert last_error is not None
        last_error.attempts = attempts  # type: ignore[attr-defined]
        raise last_error

    def _execute_json_with_retry(
        self,
        *,
        provider: BaseAIProvider,
        request: AICompletionRequest,
        retry_policy: RetryPolicy,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        attempts_prefix: list[dict[str, object]] | None = None,
        fallback_used: bool = False,
    ) -> tuple[dict[str, Any], AICompletionResult]:
        attempts: list[dict[str, object]] = []
        last_error: AIProviderError | None = None

        for attempt_index in range(retry_policy.max_retries + 1):
            self._emit_event(
                event_callback,
                {
                    "event": "provider_attempt",
                    "provider": provider.provider_name,
                    "model": provider.resolve_model(),
                    "attempt": attempt_index + 1,
                    "max_attempts": retry_policy.max_retries + 1,
                },
            )
            try:
                payload, result = self._run_provider_call_with_hard_timeout(
                    provider=provider,
                    fn=lambda: provider.complete_json(
                        request=request,
                        event_callback=event_callback,
                        attempt_number=attempt_index + 1,
                    ),
                    event_callback=event_callback,
                    attempt_number=attempt_index + 1,
                )
                result.fallback_used = fallback_used
                result.retry_count = attempt_index
                result.attempts = [
                    *(attempts_prefix or []),
                    *attempts,
                    {
                        "provider": result.provider,
                        "model": result.model,
                        "attempt": attempt_index + 1,
                        "latency_ms": result.latency_ms,
                        "outcome": "success",
                        "retryable": False,
                    },
                ]
                return payload, result
            except AIProviderError as error:
                last_error = error
                attempts.append(
                    {
                        "provider": error.provider,
                        "model": error.model,
                        "attempt": attempt_index + 1,
                        "latency_ms": 0,
                        "outcome": "failed",
                        "error_type": error.category,
                        "error_message": error.message,
                        "status_code": error.status_code,
                        "retryable": error.retryable,
                    }
                )
                if not retry_policy.should_retry(error=error, attempt_index=attempt_index):
                    error.attempts = attempts  # type: ignore[attr-defined]
                    raise
                delay_seconds = retry_policy.backoff_seconds(attempt_index=attempt_index)
                self._emit_event(
                    event_callback,
                    {
                        "event": "provider_retry_sleep",
                        "provider": error.provider,
                        "model": error.model,
                        "attempt": attempt_index + 1,
                        "max_attempts": retry_policy.max_retries + 1,
                        "retry_delay_seconds": delay_seconds,
                        "message": error.message,
                    },
                )
                time.sleep(delay_seconds)

        assert last_error is not None
        last_error.attempts = attempts  # type: ignore[attr-defined]
        raise last_error

    def _build_request(self, *, system_prompt: str, user_prompt: str) -> AICompletionRequest:
        is_reasoning_model = self.requested_model.startswith(REASONING_MODEL_PREFIXES)
        max_completion_tokens = settings.NOTE_LLM_MAX_OUTPUT_TOKENS
        if is_reasoning_model:
            max_completion_tokens = None
        return AICompletionRequest(
            requested_model=self.requested_model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=None if is_reasoning_model else settings.NOTE_LLM_TEMPERATURE,
            max_completion_tokens=max_completion_tokens,
        )

    @staticmethod
    def _format_final_error(*, attempts: list[dict[str, object]]) -> str:
        if not attempts:
            return "All AI providers failed."
        parts = [
            f"{attempt.get('provider')}[{attempt.get('model')}]: {attempt.get('error_message')}"
            for attempt in attempts
            if attempt.get("error_message")
        ]
        return " | ".join(parts) if parts else "All AI providers failed."

    def _run_provider_call_with_hard_timeout(
        self,
        *,
        provider: BaseAIProvider,
        fn: Callable[[], Any],
        event_callback: Callable[[dict[str, Any]], None] | None,
        attempt_number: int,
    ) -> Any:
        hard_timeout_seconds = max(float(getattr(provider, "timeout_seconds", 90)) + 5.0, 15.0)
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ai-{provider.provider_name}")
        request_context = copy_context()

        def runner() -> Any:
            close_old_connections()
            try:
                return fn()
            finally:
                close_old_connections()

        # Context carries safe request attribution into the timeout-isolation thread.
        future = executor.submit(request_context.run, runner)
        try:
            return future.result(timeout=hard_timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            error = AIProviderTimeoutError(
                provider=provider.provider_name,
                model=provider.resolve_model(),
                message=f"{provider.provider_name} request exceeded the hard timeout of {int(hard_timeout_seconds)} seconds.",
                retryable=True,
            )
            self._emit_event(
                event_callback,
                {
                    "event": "provider_error",
                    "provider": provider.provider_name,
                    "model": provider.resolve_model(),
                    "attempt": attempt_number,
                    "retryable": error.retryable,
                    "category": error.category,
                    "status_code": error.status_code,
                    "message": error.message,
                },
            )
            raise error from exc
        except AIProviderError:
            raise
        except Exception as exc:
            error = provider._classify_exception(exc=exc, model=provider.resolve_model())
            self._emit_event(
                event_callback,
                {
                    "event": "provider_error",
                    "provider": error.provider,
                    "model": error.model,
                    "attempt": attempt_number,
                    "retryable": error.retryable,
                    "category": error.category,
                    "status_code": error.status_code,
                    "message": error.message,
                },
            )
            raise error from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _emit_event(
        event_callback: Callable[[dict[str, Any]], None] | None,
        payload: dict[str, Any],
    ) -> None:
        if event_callback is None:
            return
        try:
            event_callback(payload)
        except Exception:
            logger.exception(
                "ai_provider_event_callback_failed",
                extra={"event": payload.get("event", ""), "provider": payload.get("provider", payload.get("to_provider", ""))},
            )
