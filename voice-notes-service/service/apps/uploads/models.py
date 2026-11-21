from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class LectureFileType(models.TextChoices):
    AUDIO = "AUDIO", "Audio"
    SLIDES = "SLIDES", "Slides"


class UploadStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    UPLOADING = "UPLOADING", "Uploading"
    UPLOADED = "UPLOADED", "Uploaded"
    FAILED = "FAILED", "Failed"


class DirectUploadSessionStatus(models.TextChoices):
    CREATED = "CREATED", "Created"
    UPLOADED = "UPLOADED", "Uploaded"
    FINALIZED = "FINALIZED", "Finalized"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Expired"
    FAILED = "FAILED", "Failed"


class DirectUploadTransport(models.TextChoices):
    DIRECT = "DIRECT", "Direct to object storage"


class LectureFile(BaseModel):
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="files")
    file_type = models.CharField(max_length=32, choices=LectureFileType.choices, db_index=True)
    original_name = models.CharField(max_length=255)
    storage_path = models.CharField(max_length=500, unique=True)
    mime_type = models.CharField(max_length=255)
    file_size = models.BigIntegerField()
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    upload_status = models.CharField(
        max_length=32,
        choices=UploadStatus.choices,
        default=UploadStatus.PENDING,
        db_index=True,
    )
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["lecture", "file_type"]),
            models.Index(fields=["lecture", "upload_status"]),
        ]

    def __str__(self) -> str:
        return self.original_name


class DirectUploadSession(BaseModel):
    """A short-lived, user-owned authorization to upload one source object."""

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="direct_upload_sessions"
    )
    lecture = models.ForeignKey(
        "lectures.Lecture",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="direct_upload_sessions",
    )
    lecture_file = models.ForeignKey(
        "uploads.LectureFile",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="direct_upload_sessions",
    )
    file_type = models.CharField(max_length=32, choices=LectureFileType.choices, db_index=True)
    object_key = models.CharField(max_length=500, unique=True)
    original_name = models.CharField(max_length=255)
    safe_extension = models.CharField(max_length=16)
    declared_content_type = models.CharField(max_length=255, blank=True)
    expected_size_bytes = models.PositiveBigIntegerField()
    declared_duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    lecture_payload = models.JSONField(default=dict, blank=True)
    queue_after_current = models.BooleanField(default=False)
    transport = models.CharField(
        max_length=16, choices=DirectUploadTransport.choices, default=DirectUploadTransport.DIRECT
    )
    status = models.CharField(
        max_length=16,
        choices=DirectUploadSessionStatus.choices,
        default=DirectUploadSessionStatus.CREATED,
        db_index=True,
    )
    authorization_expires_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    finalized_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    cleaned_at = models.DateTimeField(null=True, blank=True)
    client_upload_duration_ms = models.PositiveIntegerField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.CharField(max_length=255, blank=True)
    cleanup_attempts = models.PositiveSmallIntegerField(default=0)
    cleanup_error = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "status"], name="direct_upload_user_status_idx"),
            models.Index(fields=["status", "expires_at"], name="direct_upload_expiry_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.id}:{self.status}"


class UploadAttemptOutcome(models.TextChoices):
    IN_PROGRESS = "in_progress", "In progress"
    COMPLETED = "completed", "Completed"
    FAILED = "failed", "Failed"
    CANCELLED = "cancelled", "Cancelled by user"


class UploadAttempt(BaseModel):
    """Client-reported timing and outcome of one upload attempt, for latency diagnostics."""

    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="upload_attempts")
    client_attempt_id = models.UUIDField()
    kind = models.CharField(max_length=16, choices=LectureFileType.choices)
    method = models.CharField(max_length=16, blank=True)
    file_size = models.BigIntegerField(default=0)
    progress_percent = models.PositiveSmallIntegerField(default=0)
    elapsed_ms = models.PositiveBigIntegerField(default=0)
    speed_kbps = models.PositiveIntegerField(null=True, blank=True)
    network_type = models.CharField(max_length=16, blank=True)
    outcome = models.CharField(
        max_length=16, choices=UploadAttemptOutcome.choices, default=UploadAttemptOutcome.IN_PROGRESS
    )
    error_code = models.CharField(max_length=64, blank=True)
    # Small client-measured diagnostics for a failure (retries, stalls, hidden tab, ...).
    error_detail = models.JSONField(default=dict, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "client_attempt_id"], name="unique_upload_attempt_per_user"),
        ]
        indexes = [models.Index(fields=["outcome", "-created_at"])]


class AnonymousAudioUpload(BaseModel):
    """A class recording uploaded before signup. Held for a short window, then claimed or deleted."""

    token = models.CharField(max_length=64, unique=True)
    ip_hash = models.CharField(max_length=64, db_index=True)
    visitor_id = models.CharField(max_length=64, blank=True)
    file_name = models.CharField(max_length=255)
    declared_size = models.BigIntegerField()
    file_size = models.BigIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=127, blank=True)
    expires_at = models.DateTimeField(db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="claimed_anonymous_uploads"
    )
    lecture_id = models.UUIDField(null=True, blank=True)
    purged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["ip_hash", "created_at"], name="anon_upload_ip_created_idx")]

    def __str__(self) -> str:
        return f"anon:{self.id}"


class PendingSlidesUpload(BaseModel):
    """Slides sent while the audio of the same upload is still travelling.

    The lecture only exists once the audio is finalized, so the slides wait here (already in
    storage) and are attached by whichever side finishes last: the audio finalize, or the slides
    upload that finds the lecture already there.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="pending_slides_uploads")
    client_upload_id = models.CharField(max_length=64)
    original_name = models.CharField(max_length=255)
    storage_path = models.CharField(max_length=512)
    mime_type = models.CharField(max_length=128, blank=True, default="")
    file_size = models.PositiveBigIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "client_upload_id"], name="unique_pending_slides_per_upload"),
        ]
