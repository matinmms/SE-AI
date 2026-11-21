from __future__ import annotations

import logging

from django.conf import settings

from services.ai.providers.base import BaseAIProvider

logger = logging.getLogger("tesanote.ai")


class PrimaryAIProvider(BaseAIProvider):
    provider_name = "gapgpt"
    provider_key = "gapgpt"

    def __init__(self, *, requested_model: str) -> None:
        gapgpt_api_key = str(settings.GAPGPT_API_KEY or "").strip()
        legacy_primary_api_key = str(settings.PRIMARY_AI_API_KEY or "").strip()
        credentials_differ = (
            gapgpt_api_key
            and legacy_primary_api_key
            and gapgpt_api_key != legacy_primary_api_key
        )
        if credentials_differ:
            logger.warning(
                "gapgpt_api_key_configuration_mismatch",
                extra={
                    "selected_credential": "GAPGPT_API_KEY",
                    "ignored_credential": "PRIMARY_AI_API_KEY",
                },
            )
        super().__init__(
            requested_model=requested_model,
            api_key=gapgpt_api_key or legacy_primary_api_key,
            base_url=settings.PRIMARY_AI_BASE_URL,
            timeout_seconds=settings.NOTE_LLM_TIMEOUT_SECONDS,
            max_concurrency=settings.NOTE_PROVIDER_MAX_CONCURRENCY,
            slot_wait_seconds=settings.NOTE_PROVIDER_SLOT_WAIT_SECONDS,
            slot_poll_seconds=settings.NOTE_PROVIDER_SLOT_POLL_SECONDS,
        )
