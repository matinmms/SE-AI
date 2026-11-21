from __future__ import annotations

import json
import mimetypes
import socket
import ssl
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import certifi
from django.conf import settings

from apps.ai_pipeline.exceptions import PipelineError
from apps.audio_pipeline.providers.base import (
    BaseTranscriptionProvider,
    TranscriptionPollResult,
    TranscriptionResult,
    TranscriptionSubmission,
)
from infrastructure.queue.control import provider_concurrency_slot


class AvanegarTranscriptionProvider(BaseTranscriptionProvider):
    transcript_source = "avanegar"

    def __init__(self) -> None:
        if not settings.AVANEGAR_API_TOKEN:
            raise PipelineError(stage="TRANSCRIBING", message="AVANEGAR_API_TOKEN is not configured.")
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def _multipart_body(self, *, file_path: str) -> tuple[bytes, str]:
        boundary = f"----TesanoteBoundary{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(file_path)[0] or "audio/wav"
        filename = file_path.split("/")[-1]
        fields: list[tuple[str, str]] = [("model", "default")]

        chunks: list[bytes] = []
        for name, value in fields:
            chunks.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        with open(file_path, "rb") as file_handle:
            file_bytes = file_handle.read()
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="audio"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                file_bytes,
                b"\r\n",
                f"--{boundary}--\r\n".encode(),
            ]
        )
        return b"".join(chunks), boundary

    def _request_url(self) -> str:
        return f"{settings.AVANEGAR_BASE_URL}{settings.AVANEGAR_REQUEST_PATH}"

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        if isinstance(exc, urllib.error.URLError):
            return "timed out" in str(exc.reason).lower()
        return "timed out" in str(exc).lower()

    @staticmethod
    def _is_retryable_http_error(*, status_code: int, body: str) -> bool:
        lowered_body = body.lower()
        return status_code in {408, 425, 429, 500, 502, 503, 504} or "bad gateway" in lowered_body

    @staticmethod
    def _retry_delay_seconds(attempt_number: int) -> float:
        return settings.AVANEGAR_REQUEST_RETRY_BASE_DELAY_SECONDS * (2 ** max(0, attempt_number - 1))

    @staticmethod
    def _clean_error_excerpt(body: str) -> str:
        cleaned = " ".join(body.split())
        return cleaned[:240]

    def _unwrap_payload(self, payload: dict[str, object]) -> tuple[str, dict[str, object], dict[str, object]]:
        container = payload.get("data")
        if isinstance(container, dict):
            status = str(container.get("status") or payload.get("status") or "PENDING")
            nested = container.get("data")
            if isinstance(nested, dict):
                return status, nested, container
            return status, container, container
        return str(payload.get("status") or "PENDING"), payload, payload

    def _request_json(self, *, request: urllib.request.Request) -> dict[str, object]:
        last_error: Exception | None = None
        timeout_seconds = settings.AVANEGAR_REQUEST_TIMEOUT_SECONDS
        max_attempts = max(1, settings.AVANEGAR_REQUEST_MAX_RETRIES)
        for attempt_index in range(max_attempts):
            attempt_number = attempt_index + 1
            try:
                with provider_concurrency_slot(
                    provider_key="avanegar",
                    label="transcription provider",
                    max_concurrency=settings.AVANEGAR_PROVIDER_MAX_CONCURRENCY,
                    wait_timeout_seconds=settings.AVANEGAR_PROVIDER_SLOT_WAIT_SECONDS,
                    poll_interval_seconds=settings.AVANEGAR_PROVIDER_SLOT_POLL_SECONDS,
                    lease_seconds=max(30, timeout_seconds + 15),
                    stage="TRANSCRIBING",
                ):
                    with urllib.request.urlopen(
                        request,
                        timeout=timeout_seconds,
                        context=self.ssl_context,
                    ) as response:
                        body = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:  # pragma: no cover - provider/network dependent
                body = exc.read().decode("utf-8", errors="ignore")
                last_error = exc
                if self._is_retryable_http_error(status_code=exc.code, body=body) and attempt_number < max_attempts:
                    time.sleep(self._retry_delay_seconds(attempt_number))
                    continue
                excerpt = self._clean_error_excerpt(body) if body else str(exc.reason)
                raise PipelineError(
                    stage="TRANSCRIBING",
                    message=f"Avanegar request failed after {attempt_number} attempt(s): {excerpt}",
                ) from exc
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:  # pragma: no cover - provider/network dependent
                last_error = exc
                if self._is_timeout_error(exc) and attempt_number < max_attempts:
                    time.sleep(self._retry_delay_seconds(attempt_number))
                    continue
                if self._is_timeout_error(exc):
                    raise PipelineError(
                        stage="TRANSCRIBING",
                        message=(
                            f"Avanegar request timed out after {attempt_number} attempt(s) "
                            f"({timeout_seconds}s each)."
                        ),
                    ) from exc
                reason = exc.reason if isinstance(exc, urllib.error.URLError) else str(exc)
                raise PipelineError(
                    stage="TRANSCRIBING",
                    message=f"Avanegar request failed after {attempt_number} attempt(s): {reason}",
                ) from exc
        else:  # pragma: no cover - defensive
            raise PipelineError(stage="TRANSCRIBING", message=f"Avanegar request failed: {last_error}") from last_error

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PipelineError(stage="TRANSCRIBING", message="Avanegar returned malformed JSON.") from exc
        if not isinstance(payload, dict):
            raise PipelineError(stage="TRANSCRIBING", message="Avanegar returned an unexpected response shape.")
        payload["_tesanote_request_attempts"] = attempt_number
        payload["_tesanote_request_timeout_seconds"] = timeout_seconds
        return payload

    def _extract_text(self, payload: dict[str, object]) -> tuple[str, list[dict[str, object]], str]:
        text_candidates: list[str] = []
        language = ""
        segments: list[dict[str, object]] = []

        for key in ("text", "transcript", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text_candidates.append(value.strip())

        timestamps = payload.get("timestamps") or payload.get("segments") or payload.get("items")
        if isinstance(timestamps, list):
            for item in timestamps:
                if isinstance(item, dict):
                    segments.append(item)
                    segment_text = str(item.get("text", "")).strip()
                    if segment_text:
                        text_candidates.append(segment_text)

        ai_response = payload.get("aiResponse")
        if isinstance(ai_response, dict):
            result = ai_response.get("result")
            if isinstance(result, dict):
                result_text = str(result.get("text", "")).strip()
                if result_text:
                    text_candidates.append(result_text)
                timestamps = result.get("timestamps") or result.get("segments") or result.get("items")
                if isinstance(timestamps, list):
                    for item in timestamps:
                        if isinstance(item, dict):
                            segments.append(item)
                            segment_text = str(item.get("text", "")).strip()
                            if segment_text:
                                text_candidates.append(segment_text)

        for key in ("language", "lang"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                language = value.strip()
                break
        if not language and isinstance(ai_response, dict):
            result = ai_response.get("result")
            if isinstance(result, dict):
                for key in ("language", "lang"):
                    value = result.get(key)
                    if isinstance(value, str) and value.strip():
                        language = value.strip()
                        break

        deduped_text = "\n".join(dict.fromkeys(part for part in text_candidates if part)).strip()
        return deduped_text, segments, language

    def _completed_result_from_payload(self, payload: dict[str, object]) -> TranscriptionResult:
        status, result_payload, container = self._unwrap_payload(payload)
        text, segments, language = self._extract_text(result_payload)
        if not text:
            text, segments, language = self._extract_text(container)
        if not text:
            text, segments, language = self._extract_text(payload)
        if not text and status.casefold() == "success":
            raise PipelineError(stage="TRANSCRIBING", message="Avanegar returned success without transcript text.")
        return TranscriptionResult(
            text=text,
            raw_text=text,
            language=language,
            segments=segments,
            confidence_score=None,
            raw_response=payload,
        )

    def _submit_request(self, *, file_path: str) -> dict[str, object]:
        body, boundary = self._multipart_body(file_path=file_path)
        request = urllib.request.Request(
            url=self._request_url(),
            data=body,
            method="POST",
            headers={
                "gateway-token": settings.AVANEGAR_API_TOKEN,
                "accept": "application/json",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        return self._request_json(request=request)

    def submit(
        self,
        *,
        file_path: str,
        hotwords: list[str],
        language: str = "fa",
    ) -> TranscriptionSubmission:
        del language
        payload = self._submit_request(file_path=file_path)
        status, result_payload, _container = self._unwrap_payload(payload)
        provider_job_id = str(result_payload.get("id") or result_payload.get("jobId") or "")
        provider_trace_id = str(result_payload.get("traceId") or result_payload.get("trace_id") or "")
        if not provider_job_id:
            raise PipelineError(stage="TRANSCRIBING", message="Avanegar did not return a provider job id.")
        initial_result: TranscriptionResult | None = None
        normalized_status = status.casefold()
        if normalized_status == "success":
            try:
                initial_result = self._completed_result_from_payload(payload)
            except PipelineError:
                initial_result = None
        return TranscriptionSubmission(
            provider_job_id=provider_job_id,
            provider_trace_id=provider_trace_id,
            status=status,
            raw_response=payload,
            initial_result=initial_result,
        )

    def transcribe(self, *, file_path: str, hotwords: list[str]) -> TranscriptionResult:
        payload = self._submit_request(file_path=file_path)
        status, result_payload, container = self._unwrap_payload(payload)
        normalized_status = status.casefold()
        if normalized_status == "success":
            result = self._completed_result_from_payload(payload)
            result.raw_response = {
                **result.raw_response,
                "_request_mode": "minimal",
                "_hotwords_disabled": True,
            }
            return result
        if normalized_status in {"pending", "processing", "queued", "running"}:
            provider_job_id = str(result_payload.get("id") or result_payload.get("jobId") or "")
            provider_trace_id = str(result_payload.get("traceId") or result_payload.get("trace_id") or "")
            if not provider_job_id:
                raise PipelineError(stage="TRANSCRIBING", message="Avanegar did not return a provider job id.")
            polled = self.poll(provider_job_id=provider_job_id, provider_trace_id=provider_trace_id)
            return TranscriptionResult(
                text=polled.text,
                raw_text=polled.raw_text,
                language=polled.language,
                segments=polled.segments,
                confidence_score=polled.confidence_score,
                raw_response={
                    **polled.raw_response,
                    "_request_mode": "minimal",
                    "_hotwords_disabled": True,
                },
            )
        message = ""
        if isinstance(container, dict):
            message = str(container.get("message", "")).strip()
        raise PipelineError(stage="TRANSCRIBING", message=message or f"Avanegar returned unexpected status: {status}")

    def poll(self, *, provider_job_id: str, provider_trace_id: str) -> TranscriptionPollResult:
        query = urllib.parse.urlencode(
            {
                "id": provider_job_id,
                "traceId": provider_trace_id,
            }
        )
        request = urllib.request.Request(
            url=f"{self._request_url()}?{query}",
            method="GET",
            headers={
                "gateway-token": settings.AVANEGAR_API_TOKEN,
                "accept": "application/json",
            },
        )
        payload = self._request_json(request=request)
        status, result_payload, container = self._unwrap_payload(payload)
        text, segments, language = self._extract_text(result_payload)
        if not text:
            text, segments, language = self._extract_text(container)
        return TranscriptionPollResult(
            status=status.upper(),
            text=text,
            raw_text=text,
            language=language,
            segments=segments,
            confidence_score=None,
            raw_response=payload,
            processing_metadata={
                "provider_job_id": provider_job_id,
                "provider_trace_id": provider_trace_id,
            },
        )
