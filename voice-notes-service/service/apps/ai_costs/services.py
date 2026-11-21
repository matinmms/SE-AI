from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from django.conf import settings
from django.db import IntegrityError
from django.db.models import Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.ai_costs.models import AICostReconciliationStatus, AIProviderRequest
from apps.ai_pipeline.models import LectureProcessingJobStatus

logger = logging.getLogger("tesanote.ai_costs")
LEGACY_REQUEST_ID_CUTOFF = date(2026, 10, 15)


def extract_avalai_request_id(
    headers: Mapping[str, str] | None, *, today: date | None = None
) -> tuple[str, str]:
    """Return only a billing-safe AvalAI transaction identifier."""
    normalized = {str(key).lower(): str(value).strip() for key, value in (headers or {}).items()}
    primary = normalized.get("avalai-request-id", "")
    if primary:
        return primary, "avalai-request-id"
    legacy = normalized.get("x-request-id", "")
    if legacy and (today or timezone.now().date()) < LEGACY_REQUEST_ID_CUTOFF:
        return legacy, "x-request-id"
    return "", ""


def record_avalai_request(
    *,
    request_id: str,
    request_id_source: str,
    operation: str,
    feature: str,
    requested_model: str,
    usage: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
    http_status: int | None = None,
    attempt_number: int = 1,
) -> None:
    """Persist a pending ledger row without ever affecting a successful provider call."""
    if not request_id or not settings.AI_COST_TRACKING_ENABLED:
        return
    now = timezone.now()
    if not context:
        from apps.ai_costs.context import current_cost_context

        context = current_cost_context()
    try:
        row, created = AIProviderRequest.objects.get_or_create(
            provider="avalai",
            provider_request_id=request_id,
            defaults={
                "request_id_source": request_id_source,
                "operation": operation,
                "feature": feature,
                "requested_model": requested_model,
                "user_id": context.get("user_id") or None,
                "lecture_id": context.get("lecture_id") or None,
                "processing_job_id": context.get("processing_job_id") or None,
                "requested_at": now,
                "http_status": http_status,
                "attempt_number": attempt_number,
                "input_tokens": _int_or_none((usage or {}).get("prompt_tokens")),
                "output_tokens": _int_or_none((usage or {}).get("completion_tokens")),
                "total_tokens": _int_or_none((usage or {}).get("total_tokens")),
                "next_reconciliation_at": now
                + timedelta(seconds=settings.AI_COST_RECONCILIATION_INITIAL_DELAY_SECONDS),
            },
        )
        logger.info(
            "avalai_cost_ledger_pending",
            extra={"provider_request_id": request_id, "operation": operation, "feature": feature},
        )
        if created and row.user_id:
            # Wallet users pay add-ons (summary, quiz, tutor chat) request by request, at real cost.
            try:
                from apps.wallet.services import charge_addon_request

                charge_addon_request(row)
            except Exception:
                logger.exception("wallet_addon_charge_failed", extra={"provider_request_id": request_id})
    except (IntegrityError, ValueError, TypeError):
        logger.exception(
            "avalai_cost_ledger_record_failed", extra={"operation": operation, "feature": feature}
        )


class AvalAICostReconciliationService:
    endpoint = "https://api.avalai.ir/user/v1/transactions/lookup"

    def reconcile_due_requests(self, *, limit: int | None = None) -> dict[str, int]:
        if not settings.AI_COST_RECONCILIATION_ENABLED:
            return {"skipped": 1, "reconciled": 0, "retried": 0, "unresolved": 0}
        now = timezone.now()
        hard_cutoff = now - timedelta(days=settings.AI_COST_RECONCILIATION_HARD_MAX_AGE_DAYS)
        stale = AIProviderRequest.objects.filter(
            reconciliation_status__in=[
                AICostReconciliationStatus.PENDING,
                AICostReconciliationStatus.RETRY,
            ],
            requested_at__lt=hard_cutoff,
        )
        unresolved = stale.update(
            reconciliation_status=AICostReconciliationStatus.UNRESOLVED,
            last_error_code="hard_age_exceeded",
            last_reconciliation_at=now,
        )
        batch_size = min(limit or settings.AI_COST_RECONCILIATION_BATCH_SIZE, 1000)
        processed = reconciled = retried = 0
        while True:
            rows = list(self._due_queryset(now=now)[:batch_size])
            if not rows:
                break

            processed += len(rows)
            try:
                transactions = self.lookup_transactions([row.provider_request_id for row in rows])
            except Exception as exc:
                result = self._retry_rows(rows=rows, now=now, code=_error_code(exc))
                retried += result["retried"]
                # A provider-wide failure waits for the next off-peak run.
                break

            found = {
                str(item.get("id", "")): item for item in transactions if isinstance(item, dict)
            }
            for row in rows:
                transaction = found.get(row.provider_request_id)
                if transaction is None:
                    self._mark_retry(row=row, now=now, code="transaction_not_ready")
                    retried += 1
                    continue
                self._apply_transaction(row=row, transaction=transaction, now=now)
                reconciled += 1

        if not processed:
            return {"skipped": 0, "reconciled": 0, "retried": 0, "unresolved": unresolved}
        logger.info(
            "avalai_cost_reconciliation_complete",
            extra={"reconciled": reconciled, "retried": retried, "processed": processed},
        )
        return {
            "skipped": 0,
            "reconciled": reconciled,
            "retried": retried,
            "unresolved": unresolved,
        }

    def _due_queryset(self, *, now):
        force_cutoff = now - timedelta(hours=settings.AI_COST_RECONCILIATION_FORCE_AFTER_HOURS)
        minimum_age_cutoff = now - timedelta(
            seconds=settings.AI_COST_RECONCILIATION_INITIAL_DELAY_SECONDS
        )
        active_job_ids = AIProviderRequest.objects.filter(
            processing_job__status__in=[
                LectureProcessingJobStatus.QUEUED,
                LectureProcessingJobStatus.PROCESSING,
            ],
            requested_at__gt=force_cutoff,
        ).values_list("processing_job_id", flat=True)
        return (
            AIProviderRequest.objects.filter(
                reconciliation_status__in=[
                    AICostReconciliationStatus.PENDING,
                    AICostReconciliationStatus.RETRY,
                ],
                next_reconciliation_at__lte=now,
                requested_at__lte=minimum_age_cutoff,
            )
            .exclude(processing_job_id__in=active_job_ids)
            .order_by("next_reconciliation_at")
        )

    def lookup_transactions(self, ids: list[str]) -> list[dict[str, Any]]:
        headers = {"Authorization": f"Bearer {settings.AVALAI_API_KEY}"}
        timeout = httpx.Timeout(15.0, connect=5.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(self.endpoint, headers=headers, json={"transaction_ids": ids})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("transactions"), list):
            raise ValueError("malformed_lookup_response")
        return payload["transactions"]

    def _retry_rows(self, *, rows, now, code: str) -> dict[str, int]:
        for row in rows:
            self._mark_retry(row=row, now=now, code=code)
        logger.warning(
            "avalai_cost_reconciliation_retry", extra={"count": len(rows), "error_code": code}
        )
        return {"skipped": 0, "reconciled": 0, "retried": len(rows), "unresolved": 0}

    def _mark_retry(self, *, row: AIProviderRequest, now, code: str) -> None:
        attempts = row.reconciliation_attempts + 1
        delay_minutes = min(360, 5 * (2 ** min(attempts - 1, 6)))
        row.reconciliation_status = AICostReconciliationStatus.RETRY
        row.reconciliation_attempts = attempts
        row.last_reconciliation_at = now
        row.next_reconciliation_at = now + timedelta(minutes=delay_minutes)
        row.last_error_code = code
        row.save(
            update_fields=[
                "reconciliation_status",
                "reconciliation_attempts",
                "last_reconciliation_at",
                "next_reconciliation_at",
                "last_error_code",
                "updated_at",
            ]
        )

    def _apply_transaction(
        self, *, row: AIProviderRequest, transaction: Mapping[str, Any], now
    ) -> None:
        cost = transaction.get("cost") if isinstance(transaction.get("cost"), Mapping) else {}
        tokens = transaction.get("tokens") if isinstance(transaction.get("tokens"), Mapping) else {}
        row.provider_reported_model = str(transaction.get("model", "") or "")
        row.input_tokens = (
            _int_or_none(tokens.get("input") or tokens.get("input_tokens")) or row.input_tokens
        )
        row.output_tokens = (
            _int_or_none(tokens.get("output") or tokens.get("output_tokens")) or row.output_tokens
        )
        row.total_tokens = (
            _int_or_none(tokens.get("total") or tokens.get("total_tokens")) or row.total_tokens
        )
        provider_requested_at = parse_datetime(str(transaction.get("requested_at", "") or ""))
        if provider_requested_at is not None:
            row.provider_requested_at = provider_requested_at
        row.cost_unit = _decimal_or_none(cost.get("unit"))
        row.paid_unit = _decimal_or_none(cost.get("paid_unit"))
        row.paid_irt = _decimal_or_none(cost.get("paid_irt"))
        row.paid_grant_irt = _decimal_or_none(cost.get("paid_grant_irt"))
        row.cost_source = str(cost.get("source", "") or "")
        row.billing_metadata = {
            "status_code": transaction.get("status_code"),
            "provider": transaction.get("provider"),
            "currency": cost.get("currency"),
        }
        row.reconciliation_status = AICostReconciliationStatus.RECONCILED
        row.reconciliation_attempts += 1
        row.last_reconciliation_at = now
        row.reconciled_at = now
        row.last_error_code = ""
        row.save()


class AICostAggregationService:
    @staticmethod
    def totals(queryset):
        return queryset.filter(
            reconciliation_status=AICostReconciliationStatus.RECONCILED
        ).aggregate(
            exact_paid_irt=Coalesce(Sum("paid_irt"), Value(Decimal("0"))),
            exact_paid_grant_irt=Coalesce(Sum("paid_grant_irt"), Value(Decimal("0"))),
        )


def _int_or_none(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _error_code(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return "malformed_response" if isinstance(exc, ValueError) else type(exc).__name__.lower()
