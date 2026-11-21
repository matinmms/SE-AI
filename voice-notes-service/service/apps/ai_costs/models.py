from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class AICostReconciliationStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    RETRY = "RETRY", "Retry"
    RECONCILED = "RECONCILED", "Reconciled"
    UNRESOLVED = "UNRESOLVED", "Unresolved"


class AIProviderRequest(BaseModel):
    """One AvalAI request and its eventual provider-reported billing record."""

    provider = models.CharField(max_length=32, default="avalai")
    provider_request_id = models.CharField(max_length=128)
    request_id_source = models.CharField(max_length=32, blank=True)
    operation = models.CharField(max_length=64, blank=True)
    feature = models.CharField(max_length=64, blank=True)
    requested_model = models.CharField(max_length=160, blank=True)
    provider_reported_model = models.CharField(max_length=160, blank=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_provider_requests",
    )
    lecture = models.ForeignKey(
        "lectures.Lecture",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_provider_requests",
    )
    processing_job = models.ForeignKey(
        "ai_pipeline.LectureProcessingJob",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ai_provider_requests",
    )
    requested_at = models.DateTimeField(db_index=True)
    provider_requested_at = models.DateTimeField(null=True, blank=True)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    attempt_number = models.PositiveSmallIntegerField(default=1)
    input_tokens = models.PositiveIntegerField(null=True, blank=True)
    output_tokens = models.PositiveIntegerField(null=True, blank=True)
    total_tokens = models.PositiveIntegerField(null=True, blank=True)
    reconciliation_status = models.CharField(
        max_length=16,
        choices=AICostReconciliationStatus.choices,
        default=AICostReconciliationStatus.PENDING,
        db_index=True,
    )
    reconciliation_attempts = models.PositiveSmallIntegerField(default=0)
    next_reconciliation_at = models.DateTimeField(db_index=True)
    last_reconciliation_at = models.DateTimeField(null=True, blank=True)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=64, blank=True)
    cost_unit = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    paid_unit = models.DecimalField(max_digits=24, decimal_places=10, null=True, blank=True)
    paid_irt = models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    paid_grant_irt = models.DecimalField(max_digits=24, decimal_places=6, null=True, blank=True)
    cost_source = models.CharField(max_length=64, blank=True)
    billing_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_request_id"], name="unique_avalai_provider_request"
            )
        ]
        indexes = [
            models.Index(
                fields=["reconciliation_status", "next_reconciliation_at"], name="ai_cost_due_idx"
            ),
            models.Index(fields=["lecture", "requested_at"], name="ai_cost_lecture_idx"),
            models.Index(fields=["user", "requested_at"], name="ai_cost_user_idx"),
            models.Index(
                fields=["feature", "operation", "requested_at"], name="ai_cost_feature_idx"
            ),
        ]

    @property
    def effective_irt_consumed(self) -> Decimal | None:
        if self.paid_irt is None and self.paid_grant_irt is None:
            return None
        return (self.paid_irt or Decimal("0")) + (self.paid_grant_irt or Decimal("0"))
