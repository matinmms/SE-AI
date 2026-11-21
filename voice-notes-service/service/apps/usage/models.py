from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class UsageLogStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class UsageLog(BaseModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="usage_logs",
    )
    lecture = models.ForeignKey(
        "lectures.Lecture",
        on_delete=models.CASCADE,
        related_name="usage_logs",
    )
    user_subscription = models.ForeignKey(
        "subscriptions.UserSubscription",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usage_logs",
    )
    audio_minutes = models.PositiveIntegerField(default=0)
    # True only while this log's minutes are held on the subscription. An upload alone never holds minutes.
    minutes_reserved = models.BooleanField(default=False)
    avenger_cost = models.PositiveIntegerField(default=0)
    llm_cost = models.PositiveIntegerField(default=0)
    pdf_cost = models.PositiveIntegerField(default=0)
    storage_cost = models.PositiveIntegerField(default=0)
    total_cost = models.PositiveIntegerField(default=0)
    transcription_provider_used = models.CharField(max_length=128, blank=True)
    transcription_model_used = models.CharField(max_length=255, blank=True)
    transcription_fallback_used = models.BooleanField(default=False)
    transcription_chunks_count = models.PositiveIntegerField(default=0)
    transcription_latency_ms = models.PositiveIntegerField(default=0)
    transcription_audio_seconds = models.FloatField(default=0)
    transcription_audio_size_mb = models.FloatField(default=0)
    transcription_error_message = models.TextField(blank=True)
    status = models.CharField(
        max_length=32,
        choices=UsageLogStatus.choices,
        default=UsageLogStatus.PENDING,
        db_index=True,
    )
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["lecture", "created_at"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.lecture_id}:{self.status}"
