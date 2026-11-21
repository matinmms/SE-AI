from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class StorageArtifactClass(models.TextChoices):
    TEMPORARY = "TEMPORARY", "Temporary"
    PERSISTENT = "PERSISTENT", "Persistent"


class StoredArtifact(BaseModel):
    """Lifecycle facts for a durable object, independent of its storage backend."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stored_artifacts",
    )
    lecture = models.ForeignKey(
        "lectures.Lecture",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="stored_artifacts",
    )
    storage_path = models.CharField(max_length=500, unique=True)
    artifact_type = models.CharField(max_length=64)
    size_bytes = models.PositiveBigIntegerField(default=0)
    storage_class = models.CharField(
        max_length=16,
        choices=StorageArtifactClass.choices,
        db_index=True,
    )
    counts_toward_quota = models.BooleanField(default=False, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cleanup_started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    cleanup_attempts = models.PositiveSmallIntegerField(default=0)
    cleanup_error = models.CharField(max_length=128, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["storage_class", "expires_at", "deleted_at"],
                name="storage_lifecycle_due_idx",
            ),
            models.Index(
                fields=["user", "counts_toward_quota", "deleted_at"],
                name="storage_lifecycle_quota_idx",
            ),
            models.Index(fields=["lecture", "storage_class"], name="storage_lifecycle_lecture_idx"),
        ]
