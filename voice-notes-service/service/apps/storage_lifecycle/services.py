from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.ai_pipeline.models import LectureProcessingJobStatus
from apps.storage_lifecycle.models import StorageArtifactClass, StoredArtifact
from infrastructure.storage.base import StorageError, StorageNotFoundError
from infrastructure.storage.factory import get_storage_service

logger = logging.getLogger("tesanote.storage_lifecycle")

ACTIVE_PROCESSING_STATUSES = (
    LectureProcessingJobStatus.PENDING,
    LectureProcessingJobStatus.QUEUED,
    LectureProcessingJobStatus.PROCESSING,
)


class StorageArtifactLifecycleService:
    """Central lifecycle registry and idempotent cleanup service for durable media."""

    @staticmethod
    def _temporary_expiry(*, now=None):
        return (now or timezone.now()) + timedelta(hours=settings.TEMP_STORAGE_TTL_HOURS)

    @classmethod
    def register_temporary(
        cls,
        *,
        user_id: str,
        lecture_id: str | None,
        storage_path: str,
        size_bytes: int,
        artifact_type: str,
    ) -> StoredArtifact:
        return cls._register(
            user_id=user_id,
            lecture_id=lecture_id,
            storage_path=storage_path,
            size_bytes=size_bytes,
            artifact_type=artifact_type,
            storage_class=StorageArtifactClass.TEMPORARY,
            counts_toward_quota=False,
            expires_at=cls._temporary_expiry(),
        )

    @classmethod
    def register_persistent(
        cls,
        *,
        user_id: str,
        lecture_id: str | None,
        storage_path: str,
        size_bytes: int,
        artifact_type: str,
    ) -> StoredArtifact:
        return cls._register(
            user_id=user_id,
            lecture_id=lecture_id,
            storage_path=storage_path,
            size_bytes=size_bytes,
            artifact_type=artifact_type,
            storage_class=StorageArtifactClass.PERSISTENT,
            counts_toward_quota=True,
            expires_at=None,
        )

    @staticmethod
    def _register(
        *,
        user_id: str,
        lecture_id: str | None,
        storage_path: str,
        size_bytes: int,
        artifact_type: str,
        storage_class: str,
        counts_toward_quota: bool,
        expires_at,
    ) -> StoredArtifact:
        artifact, _ = StoredArtifact.objects.update_or_create(
            storage_path=storage_path,
            defaults={
                "user_id": user_id,
                "lecture_id": lecture_id,
                "size_bytes": max(0, int(size_bytes)),
                "artifact_type": artifact_type,
                "storage_class": storage_class,
                "counts_toward_quota": counts_toward_quota,
                "expires_at": expires_at,
                "cleanup_started_at": None,
                "cleanup_attempts": 0,
                "cleanup_error": "",
                "deleted_at": None,
            },
        )
        logger.info(
            "storage_artifact_registered",
            extra={
                "artifact_id": str(artifact.id),
                "artifact_type": artifact_type,
                "storage_class": storage_class,
                "size_bytes": artifact.size_bytes,
            },
        )
        return artifact

    @staticmethod
    def mark_deleted(*, storage_path: str) -> None:
        StoredArtifact.objects.filter(storage_path=storage_path, deleted_at__isnull=True).update(
            deleted_at=timezone.now(), cleanup_started_at=None, cleanup_error=""
        )

    @classmethod
    def schedule_temporary_expiration_for_lecture(cls, *, lecture_id: str, finished_at=None) -> int:
        """Retain sources for a full TTL after a terminal result, including failure."""
        expires_at = (finished_at or timezone.now()) + timedelta(
            hours=settings.TEMP_STORAGE_TTL_HOURS
        )
        updated = (
            StoredArtifact.objects.filter(
                lecture_id=lecture_id,
                storage_class=StorageArtifactClass.TEMPORARY,
                deleted_at__isnull=True,
            )
            .filter(Q(expires_at__isnull=True) | Q(expires_at__lt=expires_at))
            .update(expires_at=expires_at)
        )
        return updated

    @staticmethod
    def persistent_bytes_for_user(*, user_id: str) -> int:
        return int(
            StoredArtifact.objects.filter(
                user_id=user_id,
                storage_class=StorageArtifactClass.PERSISTENT,
                counts_toward_quota=True,
                deleted_at__isnull=True,
            ).aggregate(total=Sum("size_bytes"))["total"]
            or 0
        )

    @staticmethod
    def _eligible_queryset(*, now):
        claim_cutoff = now - timedelta(minutes=settings.TEMP_STORAGE_CLEANUP_CLAIM_TIMEOUT_MINUTES)
        from apps.ai_pipeline.models import LectureProcessingJob

        active_lecture_ids = LectureProcessingJob.objects.filter(
            status__in=ACTIVE_PROCESSING_STATUSES
        ).values("lecture_id")
        return (
            StoredArtifact.objects.filter(
                storage_class=StorageArtifactClass.TEMPORARY,
                expires_at__lte=now,
                deleted_at__isnull=True,
            )
            .filter(Q(cleanup_started_at__isnull=True) | Q(cleanup_started_at__lt=claim_cutoff))
            .exclude(lecture_id__in=active_lecture_ids)
            # Biggest first: the few huge source files free far more space than the
            # thousands of tiny chunk objects that share their expiry time.
            .order_by("-size_bytes", "expires_at", "created_at")
        )

    @staticmethod
    def expired_active_count(*, now) -> int:
        from apps.ai_pipeline.models import LectureProcessingJob

        active_lecture_ids = LectureProcessingJob.objects.filter(
            status__in=ACTIVE_PROCESSING_STATUSES
        ).values("lecture_id")
        return StoredArtifact.objects.filter(
            storage_class=StorageArtifactClass.TEMPORARY,
            expires_at__lte=now,
            deleted_at__isnull=True,
            lecture_id__in=active_lecture_ids,
        ).count()

    @classmethod
    def cleanup_expired_temporary_objects(
        cls, *, dry_run: bool = False, batch_size: int | None = None
    ) -> dict[str, int]:
        now = timezone.now()
        effective_batch_size = min(
            max(1, batch_size or settings.TEMP_STORAGE_CLEANUP_BATCH_SIZE), 500
        )
        eligible = cls._eligible_queryset(now=now)
        skipped_active = cls.expired_active_count(now=now)
        if dry_run:
            rows = list(eligible.values("size_bytes", "artifact_type")[:effective_batch_size])
            return {
                "dry_run": 1,
                "eligible": len(rows),
                "eligible_bytes": sum(int(row["size_bytes"]) for row in rows),
                "skipped_active": skipped_active,
                "deleted": 0,
                "missing": 0,
                "failed": 0,
            }
        if not settings.TEMP_STORAGE_CLEANUP_ENABLED:
            return {
                "disabled": 1,
                "eligible": 0,
                "eligible_bytes": 0,
                "skipped_active": skipped_active,
                "deleted": 0,
                "missing": 0,
                "failed": 0,
            }

        with transaction.atomic():
            claimed = list(eligible.select_for_update(skip_locked=True)[:effective_batch_size])
            if claimed:
                StoredArtifact.objects.filter(id__in=[artifact.id for artifact in claimed]).update(
                    cleanup_started_at=now
                )

        result = {
            "disabled": 0,
            "eligible": len(claimed),
            "eligible_bytes": sum(artifact.size_bytes for artifact in claimed),
            "skipped_active": skipped_active,
            "deleted": 0,
            "missing": 0,
            "failed": 0,
        }
        storage = get_storage_service()
        result["throttled"] = 0
        if hasattr(storage, "delete_many"):
            cls._delete_in_batches(storage=storage, claimed=claimed, now=now, result=result)
        else:
            for artifact in claimed:
                cls._delete_one(storage=storage, artifact=artifact, now=now, result=result)

        logger.info("temporary_storage_cleanup_complete", extra=result)
        return result

    @classmethod
    def _delete_in_batches(cls, *, storage, claimed, now, result) -> None:
        try:
            outcome = storage.delete_many([artifact.storage_path for artifact in claimed])
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            for artifact in claimed:
                cls._mark_cleanup_failed(artifact_id=artifact.id, error=exc.__class__.__name__)
                result["failed"] += 1
            return
        for artifact in claimed:
            state = outcome.get(artifact.storage_path, "failed")
            if state == "deleted":
                cls._mark_cleanup_deleted(artifact_id=artifact.id, now=now)
                result["deleted"] += 1
            elif state == "throttled":
                # Not the object's fault: release the claim without counting an attempt.
                StoredArtifact.objects.filter(id=artifact.id, deleted_at__isnull=True).update(
                    cleanup_started_at=None
                )
                result["throttled"] += 1
            else:
                cls._mark_cleanup_failed(artifact_id=artifact.id, error="StorageProviderError")
                result["failed"] += 1

    @classmethod
    def _delete_one(cls, *, storage, artifact, now, result) -> None:
        try:
            storage.delete(artifact.storage_path)
        except StorageNotFoundError:
            cls._mark_cleanup_deleted(artifact_id=artifact.id, now=now)
            result["missing"] += 1
        except StorageError as exc:
            cls._mark_cleanup_failed(artifact_id=artifact.id, error=exc.__class__.__name__)
            result["failed"] += 1
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            cls._mark_cleanup_failed(artifact_id=artifact.id, error=exc.__class__.__name__)
            result["failed"] += 1
        else:
            cls._mark_cleanup_deleted(artifact_id=artifact.id, now=now)
            result["deleted"] += 1

    @staticmethod
    def _mark_cleanup_deleted(*, artifact_id, now) -> None:
        StoredArtifact.objects.filter(id=artifact_id, deleted_at__isnull=True).update(
            deleted_at=now, cleanup_started_at=None, cleanup_error=""
        )

    @staticmethod
    def _mark_cleanup_failed(*, artifact_id, error: str) -> None:
        StoredArtifact.objects.filter(id=artifact_id, deleted_at__isnull=True).update(
            cleanup_started_at=None,
            cleanup_attempts=F("cleanup_attempts") + 1,
            cleanup_error=error[:128],
        )
