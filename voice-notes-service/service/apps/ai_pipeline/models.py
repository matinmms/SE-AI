from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.common.models import BaseModel


class LectureProcessingJobStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    QUEUED = "QUEUED", "Queued"
    PROCESSING = "PROCESSING", "Processing"
    SUCCEEDED = "SUCCEEDED", "Succeeded"
    FAILED = "FAILED", "Failed"


class LectureProcessingFailureCategory(models.TextChoices):
    DISPATCH_FAILED = "DISPATCH_FAILED", "Dispatch failed"
    TRANSCRIPTION_FAILED = "TRANSCRIPTION_FAILED", "Transcription failed"
    GENERATION_FAILED = "GENERATION_FAILED", "Generation failed"
    PDF_FAILED = "PDF_FAILED", "PDF failed"
    INTERNAL_ERROR = "INTERNAL_ERROR", "Internal error"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED", "Recovery required"


class LectureProcessingJob(BaseModel):
    """One durable processing attempt for a lecture."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="lecture_processing_jobs",
    )
    lecture = models.ForeignKey(
        "lectures.Lecture",
        on_delete=models.CASCADE,
        related_name="processing_jobs",
    )
    usage_log = models.ForeignKey(
        "usage.UsageLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_jobs",
    )
    attempt = models.PositiveIntegerField(default=1)
    idempotency_key = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16,
        choices=LectureProcessingJobStatus.choices,
        default=LectureProcessingJobStatus.QUEUED,
        db_index=True,
    )
    stage = models.CharField(max_length=64, default="QUEUED")
    progress = models.PositiveSmallIntegerField(default=0)
    celery_task_id = models.CharField(max_length=255, blank=True, db_index=True)
    queued_at = models.DateTimeField()
    # Scheduler metadata is additive: QUEUED still represents the existing
    # per-user active reservation, while these fields describe global dispatch.
    scheduler_managed = models.BooleanField(default=False, db_index=True)
    scheduler_ready_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduler_claimed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduler_dispatched_at = models.DateTimeField(null=True, blank=True, db_index=True)
    scheduler_dispatch_attempts = models.PositiveSmallIntegerField(default=0)
    scheduler_last_dispatch_error = models.CharField(max_length=128, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    failure_category = models.CharField(
        max_length=32,
        choices=LectureProcessingFailureCategory.choices,
        blank=True,
    )
    # Phase 12: heartbeat/recovery for jobs that have actually begun executing
    # (PROCESSING) and then go silent. execution_generation is the fencing
    # token: it is bumped only when recovery decides an attempt is dead, so an
    # old attempt that later resumes carries a stale generation number and is
    # rejected at the authoritative write boundaries (mark_succeeded_for_lecture
    # / mark_failed_for_lecture). It is intentionally distinct from `attempt`,
    # which identifies a whole new user-initiated processing request.
    execution_generation = models.PositiveIntegerField(default=1)
    last_heartbeat_at = models.DateTimeField(null=True, blank=True)
    recovery_count = models.PositiveSmallIntegerField(default=0)
    last_recovery_reason = models.CharField(max_length=64, blank=True)
    last_recovery_at = models.DateTimeField(null=True, blank=True)
    # Phase 16: which registered processing-worker host this dispatch was
    # assigned to (see apps.ai_pipeline.capacity.DistributedCapacityController).
    # Blank when the fair scheduler is running in single-host mode
    # (DISTRIBUTED_CAPACITY_ENABLED=False) — existing behavior is unaffected.
    assigned_worker_host_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["lecture", "attempt"],
                name="unique_processing_job_attempt",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status__in=["QUEUED", "PROCESSING"]),
                name="one_active_processing_job_per_user",
            ),
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(status="PENDING"),
                name="one_pending_processing_job_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["lecture", "status"], name="ai_job_lecture_status_idx"),
            models.Index(fields=["user", "status", "created_at"], name="ai_job_user_status_idx"),
            models.Index(
                fields=["scheduler_managed", "status", "scheduler_ready_at"],
                name="ai_job_scheduler_ready_idx",
            ),
            models.Index(
                fields=["scheduler_managed", "scheduler_claimed_at"],
                name="ai_job_scheduler_claim_idx",
            ),
            models.Index(
                fields=["status", "last_heartbeat_at"],
                name="ai_job_heartbeat_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.lecture_id}:attempt-{self.attempt}:{self.status}"


class FairSchedulerDispatchControl(models.Model):
    """A single durable row used to serialize global scheduler slot claims."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Fair scheduler dispatch control"
        verbose_name_plural = "Fair scheduler dispatch controls"
