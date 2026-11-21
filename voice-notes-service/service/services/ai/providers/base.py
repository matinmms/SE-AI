from __future__ import annotations

import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

import httpx
from django.conf import settings
from openai import OpenAI

from apps.ai_pipeline.exceptions import PipelineError
from infrastructure.observability.scaling import measure_scaling_stage
from infrastructure.queue.control import provider_concurrency_slot, provider_rate_limit_gate
from services.ai.exceptions import (
    AIProviderAuthenticationError,
    AIProviderError,
    AIProviderMalformedResponseError,
    AIProviderRateLimitError,
    AIProviderServerError,
    AIProviderTimeoutError,
    AIProviderUnavailableError,
)

logger = logging.getLogger("tesanote.ai")

# OpenAI's reasoning-model families (gpt-5, gpt-6, ...) reject a custom
# `temperature` and use their own completion-length budgeting, so requests
# to these must omit both rather than sending the note-generation defaults.
REASONING_MODEL_PREFIXES: tuple[str, ...] = ("gpt-5", "gpt-6")

_SHARED_CLIENTS: dict[tuple[str, str, int, int], OpenAI] = {}
_SHARED_CLIENT_LOCK = threading.Lock()


@dataclass
class ProviderAttempt:
    provider: str
    model: str
    attempt: int
    latency_ms: int
    outcome: str
    error_type: str = ""
    error_message: str = ""
    status_code: int | None = None
    retryable: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class AICompletionRequest:
    requested_model: str
    system_prompt: str
    user_prompt: str
    temperature: float | None
    max_completion_tokens: int | None
    cost_context: dict[str, object] = field(default_factory=dict)
    # Optional, only for reasoning models (gpt-5/gpt-6): "none"/"low"/... lowers the hidden
    # reasoning tokens that are billed as output. None leaves the provider default untouched.
    reasoning_effort: str | None = None

    @property
    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.user_prompt},
        ]


@dataclass
class AICompletionResult:
    content: str
    usage: dict[str, int]
    model: str
    provider: str = ""
    fallback_used: bool = False
    latency_ms: int = 0
    retry_count: int = 0
    attempts: list[dict[str, object]] = field(default_factory=list)
    provider_request_id: str = ""

    @property
    def observability(self) -> dict[str, object]:
        return {
            "provider_used": self.provider,
            "model_used": self.model,
            "fallback_used": self.fallback_used,
            "generation_latency_ms": self.latency_ms,
            "retry_count": self.retry_count,
            "attempts": self.attempts,
        }


class BaseAIProvider(ABC):
    provider_name: str = "base"
    provider_key: str = "base"
    model_override: str | None = None

    def __init__(
        self,
        *,
        requested_model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: int,
        max_concurrency: int,
        slot_wait_seconds: int,
        slot_poll_seconds: float,
    ) -> None:
        if not api_key:
            raise PipelineError(stage="NOTE_GENERATION", message=f"{self.provider_name} API key is not configured.")
        self.requested_model = requested_model
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = int(timeout_seconds)
        self.max_concurrency = int(max_concurrency)
        self.slot_wait_seconds = int(slot_wait_seconds)
        self.slot_poll_seconds = float(slot_poll_seconds)
    def authorization_header(self) -> str | None:
        """Full value of the Authorization header when the provider does not use "Bearer <key>"."""
        return None

    def resolve_model(self) -> str:
        return self.model_override or self.requested_model

    def complete(
        self,
        *,
        request: AICompletionRequest,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        attempt_number: int = 1,
    ) -> AICompletionResult:
        started = time.monotonic()
        prepared_request = self._prepare_request(request)
        model = self.resolve_model()
        try:
            with measure_scaling_stage(
                stage="provider_completion",
                provider=self.provider_name,
                model=model,
                retry_count=max(0, attempt_number - 1),
            ) as measurement:
                provider_rate_limit_gate(
                    provider_key=f"note-{self.provider_key}",
                    estimated_tokens=self._estimate_total_tokens(prepared_request),
                    rpm_limit=self.rate_limit_rpm(),
                    tpm_limit=self.rate_limit_tpm(),
                    wait_timeout_seconds=self.rate_limit_wait_seconds(),
                    poll_interval_seconds=self.rate_limit_poll_seconds(),
                    stage="NOTE_GENERATION",
                    event_callback=event_callback,
                )
                with provider_concurrency_slot(
                    provider_key=f"note-{self.provider_key}",
                    label=f"{self.provider_name} note generation model",
                    max_concurrency=self.max_concurrency,
                    wait_timeout_seconds=self.slot_wait_seconds,
                    poll_interval_seconds=self.slot_poll_seconds,
                    lease_seconds=max(30, self.timeout_seconds + 30),
                    stage="NOTE_GENERATION",
                    event_callback=event_callback,
                ):
                    client = self._shared_client()
                    raw_response = client.chat.completions.with_raw_response.create(
                        **self._build_request_payload(request=prepared_request, model=model)
                    )
                    response = raw_response.parse()
                measurement.add(provider_latency_ms=round((time.monotonic() - started) * 1000))
        except Exception as exc:  # pragma: no cover - provider/network dependent
            provider_error = self._classify_exception(exc=exc, model=model)
            latency_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "ai_provider_request_failed",
                extra={
                    "provider": self.provider_name,
                    "model": model,
                    "attempt": attempt_number,
                    "latency_ms": latency_ms,
                    "retryable": provider_error.retryable,
                    "category": provider_error.category,
                    "status_code": provider_error.status_code,
                    "error_message": provider_error.message,
                },
            )
            self._emit_event_callback(
                event_callback,
                {
                    "event": "provider_error",
                    "provider": self.provider_name,
                    "model": model,
                    "attempt": attempt_number,
                    "retryable": provider_error.retryable,
                    "category": provider_error.category,
                    "status_code": provider_error.status_code,
                    "message": provider_error.message,
                },
            )
            raise provider_error from exc

        payload = self._to_dict(response)
        content = self._resolve_content(payload)
        if not content:
            error = AIProviderMalformedResponseError(
                provider=self.provider_name,
                model=model,
                message=f"{self.provider_name} returned an empty response.",
                retryable=True,
            )
            self._emit_event_callback(
                event_callback,
                {
                    "event": "provider_error",
                    "provider": self.provider_name,
                    "model": model,
                    "attempt": attempt_number,
                    "retryable": error.retryable,
                    "category": error.category,
                    "status_code": error.status_code,
                    "message": error.message,
                },
            )
            raise error
        usage = getattr(response, "usage", None)
        latency_ms = int((time.monotonic() - started) * 1000)
        request_id, request_id_source = self._provider_request_id(raw_response=raw_response)
        if self.provider_name == "avalai" and request_id:
            # Ledger persistence is intentionally best-effort and has no billing lookup here.
            from apps.ai_costs.context import current_cost_context
            from apps.ai_costs.services import record_avalai_request

            cost_context = prepared_request.cost_context or current_cost_context()
            record_avalai_request(
                request_id=request_id,
                request_id_source=request_id_source,
                operation=str(cost_context.get("operation") or cost_context.get("feature") or "note_generation"),
                feature=str(cost_context.get("feature") or "note_generation"),
                requested_model=model,
                usage={
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "total_tokens": getattr(usage, "total_tokens", 0),
                },
                context=cost_context,
                attempt_number=attempt_number,
            )
        logger.info(
            "ai_provider_request_succeeded",
            extra={
                "provider": self.provider_name,
                "model": model,
                "attempt": attempt_number,
                "latency_ms": latency_ms,
            },
        )
        return AICompletionResult(
            content=content,
            usage={
                "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
                # Detail counters: hidden reasoning is billed as output, cached input at a lower rate.
                "reasoning_tokens": int(
                    getattr(getattr(usage, "completion_tokens_details", None), "reasoning_tokens", 0) or 0
                ),
                "cached_tokens": int(
                    getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0) or 0
                ),
            },
            model=model,
            provider=self.provider_name,
            latency_ms=latency_ms,
            provider_request_id=request_id,
        )

    @staticmethod
    def _provider_request_id(*, raw_response: Any) -> tuple[str, str]:
        from apps.ai_costs.services import extract_avalai_request_id

        headers = getattr(raw_response, "headers", None)
        return extract_avalai_request_id(headers)

    def complete_json(
        self,
        *,
        request: AICompletionRequest,
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        attempt_number: int = 1,
    ) -> tuple[dict[str, Any], AICompletionResult]:
        result = self.complete(request=request, event_callback=event_callback, attempt_number=attempt_number)
        payload_text = self._extract_json(result.content)
        try:
            payload = self._loads_tolerant(payload_text)
        except json.JSONDecodeError as exc:
            error = AIProviderMalformedResponseError(
                provider=self.provider_name,
                model=result.model,
                message=f"{self.provider_name} did not return valid JSON.",
                retryable=True,
            )
            self._emit_event_callback(
                event_callback,
                {
                    "event": "provider_error",
                    "provider": self.provider_name,
                    "model": result.model,
                    "attempt": attempt_number,
                    "retryable": error.retryable,
                    "category": error.category,
                    "status_code": error.status_code,
                    "message": error.message,
                },
            )
            raise error from exc
        if not isinstance(payload, dict):
            error = AIProviderMalformedResponseError(
                provider=self.provider_name,
                model=result.model,
                message=f"{self.provider_name} JSON output must be an object.",
                retryable=True,
            )
            self._emit_event_callback(
                event_callback,
                {
                    "event": "provider_error",
                    "provider": self.provider_name,
                    "model": result.model,
                    "attempt": attempt_number,
                    "retryable": error.retryable,
                    "category": error.category,
                    "status_code": error.status_code,
                    "message": error.message,
                },
            )
            raise error
        return payload, result

    def _build_request_payload(self, *, request: AICompletionRequest, model: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": request.messages,
        }
        if not model.startswith(REASONING_MODEL_PREFIXES):
            payload["temperature"] = request.temperature
            if request.max_completion_tokens:
                payload["max_completion_tokens"] = request.max_completion_tokens
        elif request.reasoning_effort:
            payload["reasoning_effort"] = request.reasoning_effort
        return payload

    def _prepare_request(self, request: AICompletionRequest) -> AICompletionRequest:
        return request

    def _estimate_total_tokens(self, request: AICompletionRequest) -> int:
        prompt_chars = len(request.system_prompt) + len(request.user_prompt)
        prompt_tokens = max(1, int(prompt_chars / 4))
        completion_tokens = int(request.max_completion_tokens or 2048)
        return prompt_tokens + completion_tokens

    def rate_limit_rpm(self) -> int:
        return 0

    def rate_limit_tpm(self) -> int:
        return 0

    def rate_limit_wait_seconds(self) -> int:
        return self.slot_wait_seconds

    def rate_limit_poll_seconds(self) -> float:
        return self.slot_poll_seconds

    def _timeout(self) -> httpx.Timeout:
        seconds = float(self.timeout_seconds)
        return httpx.Timeout(
            timeout=seconds,
            connect=min(15.0, seconds),
            read=seconds,
            write=seconds,
            pool=seconds,
        )

    def _shared_client(self) -> OpenAI:
        """One pooled client per (endpoint, key, timeout), reused across calls.

        A per-call client threw its connection pool away after every request, so
        every note-generation call paid a fresh DNS lookup, TCP handshake and TLS
        handshake to the provider — on the critical path, dozens of times per
        note. The request payload is unchanged, so answers are unaffected.
        """
        auth_header = self.authorization_header()
        key = (self.base_url, self.api_key, self.timeout_seconds, self.max_concurrency, auth_header)
        with _SHARED_CLIENT_LOCK:
            client = _SHARED_CLIENTS.get(key)
            if client is None:
                hooks: dict[str, list[Callable[[httpx.Request], None]]] = {}
                if auth_header:

                    def _set_authorization(request: httpx.Request, value: str = auth_header) -> None:
                        request.headers["Authorization"] = value

                    hooks["request"] = [_set_authorization]
                client = OpenAI(
                    base_url=self.base_url,
                    api_key=self.api_key,
                    timeout=self._timeout(),
                    max_retries=0,
                    http_client=httpx.Client(
                        event_hooks=hooks,
                        timeout=self._timeout(),
                        limits=httpx.Limits(
                            max_connections=max(8, self.max_concurrency * 4),
                            max_keepalive_connections=max(4, self.max_concurrency * 2),
                            keepalive_expiry=90.0,
                        ),
                    ),
                )
                _SHARED_CLIENTS[key] = client
            return client

    def _emit_event_callback(
        self,
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
                extra={"provider": payload.get("provider", self.provider_name), "event": payload.get("event", "")},
            )

    @classmethod
    def _to_dict(cls, payload: Any) -> dict[str, Any]:
        if hasattr(payload, "model_dump"):
            dumped = payload.model_dump()
            if isinstance(dumped, dict):
                return dumped
        if isinstance(payload, dict):
            return payload
        result: dict[str, Any] = {}
        for name in dir(payload):
            if name.startswith("_"):
                continue
            value = getattr(payload, name)
            if callable(value):
                continue
            result[name] = value
        return result

    @classmethod
    def _resolve_content(cls, payload: Any) -> str:
        if isinstance(payload, str):
            return payload.strip()
        if hasattr(payload, "model_dump"):
            dumped = payload.model_dump()
            if dumped is not None:
                return cls._resolve_content(dumped)
        if hasattr(payload, "__dict__") and not isinstance(payload, type):
            try:
                return cls._resolve_content(vars(payload))
            except TypeError:
                pass
        if isinstance(payload, list):
            parts = [cls._resolve_content(item) for item in payload]
            return "\n".join(part for part in parts if part).strip()
        if isinstance(payload, dict):
            for key in ("content", "text", "output_text", "refusal", "reasoning_content"):
                value = payload.get(key)
                text = cls._resolve_content(value)
                if text:
                    return text
            for key in ("choices", "message", "delta", "output"):
                value = payload.get(key)
                if value:
                    text = cls._resolve_content(value)
                    if text:
                        return text
            for value in payload.values():
                if isinstance(value, (dict, list)):
                    text = cls._resolve_content(value)
                    if text:
                        return text
        return ""

    @staticmethod
    def _loads_tolerant(text: str) -> Any:
        """Parse model JSON, repairing the damage small models commonly cause.

        Handles raw control characters inside strings, trailing commas and a
        reply cut off mid-document (unterminated string or unclosed brackets).
        Raises json.JSONDecodeError when the text cannot be recovered.
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError as first_error:
            error = first_error
        candidate = re.sub(r",(\s*[}\]])", r"\1", text)
        try:
            return json.loads(candidate, strict=False)
        except json.JSONDecodeError:
            pass
        stack: list[str] = []
        in_string = False
        escaped = False
        for char in candidate:
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char in "{[":
                stack.append("}" if char == "{" else "]")
            elif char in "}]" and stack:
                stack.pop()
        if not stack and not in_string:
            raise error
        repaired = candidate
        if escaped:
            repaired = repaired[:-1]
        if in_string:
            repaired += '"'
        repaired = re.sub(r"[,:\s]+$", "", repaired)
        if repaired.endswith('"') and stack and stack[-1] == "}":
            # A dangling key with no value cannot be closed; drop it.
            key_start = repaired.rfind('"', 0, len(repaired) - 1)
            before = repaired[:key_start].rstrip() if key_start > 0 else ""
            if before.endswith(("{", ",")):
                repaired = before.rstrip(",")
        repaired += "".join(reversed(stack))
        try:
            return json.loads(repaired, strict=False)
        except json.JSONDecodeError:
            raise error from None

    @staticmethod
    def _extract_json(content: str) -> str:
        candidate = content.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", candidate, flags=re.DOTALL)
        if fenced:
            return fenced.group(1)
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end != -1 and end > start:
            return candidate[start : end + 1]
        return candidate

    @staticmethod
    def _sanitize_error_message(message: str) -> str:
        lowered = (message or "").lower()
        if "<!doctype html" in lowered and "502" in lowered:
            return "Provider returned HTTP 502 Bad Gateway."
        if "timed out" in lowered or "timeout" in lowered:
            return "Request timed out."
        return re.sub(r"\s+", " ", message or "").strip() or "Unknown provider error."

    @staticmethod
    def _provider_error_fields(exc: Exception) -> tuple[str, str, str]:
        error_payload: Any = getattr(exc, "body", None)
        if not isinstance(error_payload, dict):
            response = getattr(exc, "response", None)
            if response is not None:
                try:
                    error_payload = response.json()
                except Exception:
                    error_payload = None

        if isinstance(error_payload, dict):
            nested_error = error_payload.get("error", error_payload)
            if isinstance(nested_error, dict):
                return (
                    str(nested_error.get("type", "") or "").strip().lower(),
                    str(nested_error.get("code", "") or "").strip().lower(),
                    str(nested_error.get("message", "") or "").strip().lower(),
                )

        raw_message = str(exc or "")

        def field(name: str) -> str:
            match = re.search(
                rf"[\"']{name}[\"']\s*:\s*[\"']([^\"']*)[\"']",
                raw_message,
                flags=re.IGNORECASE,
            )
            return match.group(1).strip().lower() if match else ""

        return field("type"), field("code"), field("message")

    @classmethod
    def _classify_exception(cls, *, exc: Exception, model: str) -> AIProviderError:
        message = cls._sanitize_error_message(str(exc))
        lowered = message.lower()
        provider_error_type, provider_error_code, provider_error_message = cls._provider_error_fields(
            exc
        )
        provider_error_text = " ".join(
            part
            for part in (provider_error_type, provider_error_code, provider_error_message, lowered)
            if part
        )
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        if status_code is None:
            status_match = re.search(r"\berror code:\s*(\d{3})\b", lowered)
            if status_match:
                status_code = int(status_match.group(1))
        quota_markers = (
            "pre_consume_token_quota_failed",
            "insufficient_quota",
            "quota",
            "credit",
            "balance",
            "billing",
        )

        if isinstance(exc, (httpx.TimeoutException,)) or "timed out" in lowered or "timeout" in lowered:
            return AIProviderTimeoutError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=True,
                status_code=status_code,
            )
        if status_code == 429 or "rate limit" in lowered or "api_limit" in lowered or "429" in lowered:
            return AIProviderRateLimitError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=True,
                status_code=status_code,
            )
        if status_code == 403 and any(marker in provider_error_text for marker in quota_markers):
            return AIProviderRateLimitError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=False,
                status_code=status_code,
            )
        authentication_markers = (
            "invalid api key",
            "invalid_api_key",
            "incorrect api key",
            "unauthorized",
            "authentication failed",
            "invalid authentication",
            "missing authentication",
            "revoked token",
            "expired token",
            "token invalid",
            "api key disabled",
            "account disabled",
        )
        has_authentication_marker = any(
            marker in provider_error_text for marker in authentication_markers
        )
        if status_code in {401, 403} and has_authentication_marker:
            return AIProviderAuthenticationError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=False,
                status_code=status_code,
            )
        if (
            status_code == 403
            and cls.provider_key == "gapgpt"
            and provider_error_type == "api_provider_error"
            and provider_error_message.startswith("request failed")
        ):
            return AIProviderUnavailableError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=True,
                status_code=status_code,
            )
        if status_code in {500, 502, 503, 504} or any(marker in lowered for marker in ["bad gateway", "temporarily inaccessible", "server error", "<!doctype html"]):
            return AIProviderServerError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=True,
                status_code=status_code,
            )
        if status_code in {401, 403}:
            return AIProviderAuthenticationError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=False,
                status_code=status_code,
            )
        # RemoteProtocolError is how a keep-alive connection that the provider
        # closed in the meantime surfaces; it is a reconnect, not a bad request.
        if isinstance(
            exc, (httpx.ConnectError, httpx.NetworkError, httpx.RemoteProtocolError)
        ) or any(
            marker in lowered
            for marker in ["connection", "unavailable", "dns", "refused", "disconnected"]
        ):
            return AIProviderUnavailableError(
                provider=cls.provider_name,
                model=model,
                message=message,
                retryable=True,
                status_code=status_code,
            )
        return AIProviderError(
            provider=cls.provider_name,
            model=model,
            message=message,
            retryable=False,
            status_code=status_code,
        )
