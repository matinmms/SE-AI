from __future__ import annotations

import re

from django.conf import settings

from services.ai.providers.base import AICompletionRequest, BaseAIProvider


class AvalAIProvider(BaseAIProvider):
    provider_name = "avalai"
    provider_key = "avalai"

    def __init__(self, *, requested_model: str, model_override: str | None = None) -> None:
        super().__init__(
            requested_model=requested_model,
            api_key=settings.AVALAI_API_KEY,
            base_url=settings.AVALAI_BASE_URL,
            timeout_seconds=settings.AVALAI_TIMEOUT_SECONDS,
            max_concurrency=settings.AVALAI_PROVIDER_MAX_CONCURRENCY,
            slot_wait_seconds=settings.AVALAI_PROVIDER_SLOT_WAIT_SECONDS,
            slot_poll_seconds=settings.AVALAI_PROVIDER_SLOT_POLL_SECONDS,
        )
        self.model_override = model_override or settings.AVALAI_MODEL

    def _prepare_request(self, request: AICompletionRequest) -> AICompletionRequest:
        user_prompt = self._compact_user_prompt(str(request.user_prompt or ""))
        max_output_tokens = request.max_completion_tokens
        if max_output_tokens is None:
            max_output_tokens = settings.AVALAI_TARGET_MAX_OUTPUT_TOKENS
        else:
            max_output_tokens = min(int(max_output_tokens), int(settings.AVALAI_TARGET_MAX_OUTPUT_TOKENS))
        return AICompletionRequest(
            requested_model=request.requested_model,
            system_prompt=str(request.system_prompt or ""),
            user_prompt=user_prompt,
            temperature=request.temperature,
            max_completion_tokens=max_output_tokens,
            cost_context=request.cost_context,
            reasoning_effort=request.reasoning_effort,
        )

    def rate_limit_rpm(self) -> int:
        return int(settings.AVALAI_RPM_LIMIT)

    def rate_limit_tpm(self) -> int:
        return int(settings.AVALAI_TPM_LIMIT)

    def rate_limit_wait_seconds(self) -> int:
        return int(settings.AVALAI_RATE_LIMIT_WAIT_SECONDS)

    def rate_limit_poll_seconds(self) -> float:
        return float(settings.AVALAI_RATE_LIMIT_POLL_SECONDS)

    def _compact_user_prompt(self, prompt: str) -> str:
        compacted = str(prompt or "")
        compacted = self._limit_section(
            text=compacted,
            start_marker="Slide context:\n",
            end_marker="\nTranscript chunk outline:\n",
            max_chars=int(settings.AVALAI_SLIDE_CONTEXT_CHAR_LIMIT),
        )
        compacted = self._limit_section(
            text=compacted,
            start_marker="Relevant slide pages for this batch:\n",
            end_marker="\nYou are processing part",
            max_chars=int(settings.AVALAI_SLIDE_CONTEXT_CHAR_LIMIT),
        )
        compacted = self._limit_section(
            text=compacted,
            start_marker="Relevant slide pages:\n",
            end_marker="\nReturn JSON with keys",
            max_chars=int(settings.AVALAI_SLIDE_CONTEXT_CHAR_LIMIT),
        )
        compacted = self._limit_section(
            text=compacted,
            start_marker="Transcript chunk outline:\n",
            end_marker="\nFull transcript text:\n",
            max_chars=int(settings.AVALAI_TRANSCRIPT_OUTLINE_CHAR_LIMIT),
        )
        compacted = re.sub(r"\n{3,}", "\n\n", compacted)
        target_chars = int(settings.AVALAI_TARGET_MAX_INPUT_TOKENS) * 4
        if len(compacted) > target_chars:
            compacted = self._replace_section_with_placeholder(
                text=compacted,
                start_marker="Transcript chunk outline:\n",
                end_marker="\nFull transcript text:\n",
                placeholder="Outline removed for note provider budget.\n",
            )
        if len(compacted) > target_chars:
            compacted = self._replace_section_with_placeholder(
                text=compacted,
                start_marker="Slide context:\n",
                end_marker="\nTranscript chunk outline:\n",
                placeholder="Slide context minimized for note provider budget.\n",
            )
        if len(compacted) > target_chars:
            compacted = self._replace_section_with_placeholder(
                text=compacted,
                start_marker="Relevant slide pages for this batch:\n",
                end_marker="\nYou are processing part",
                placeholder="Relevant slide pages minimized for note provider budget.\n",
            )
        return compacted.strip()

    def _limit_section(self, *, text: str, start_marker: str, end_marker: str, max_chars: int) -> str:
        start = text.find(start_marker)
        if start == -1:
            return text
        content_start = start + len(start_marker)
        end = text.find(end_marker, content_start)
        if end == -1:
            return text
        section = text[content_start:end]
        if len(section) <= max_chars:
            return text
        trimmed = section[:max_chars].rsplit("\n", 1)[0].strip()
        if not trimmed:
            trimmed = section[:max_chars].strip()
        suffix = "\n[trimmed for note provider budget]\n"
        return f"{text[:content_start]}{trimmed}{suffix}{text[end:]}"

    def _replace_section_with_placeholder(self, *, text: str, start_marker: str, end_marker: str, placeholder: str) -> str:
        start = text.find(start_marker)
        if start == -1:
            return text
        content_start = start + len(start_marker)
        end = text.find(end_marker, content_start)
        if end == -1:
            return text
        return f"{text[:content_start]}{placeholder}{text[end:]}"
