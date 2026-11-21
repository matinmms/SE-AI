from __future__ import annotations

from dataclasses import asdict, dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import BigIntegerField, Count, Q, Sum, Value
from django.db.models.functions import Coalesce

from apps.ai_pipeline.models import LectureProcessingJobStatus
from apps.lectures.models import Lecture
from apps.storage_lifecycle.models import StoredArtifact
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from infrastructure.storage.base import StorageError, StorageNotFoundError
from infrastructure.storage.factory import get_storage_service


class StorageDeletionError(RuntimeError):
    pass


class StorageLectureActiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class StorageQuotaSnapshot:
    used_bytes: int
    quota_bytes: int
    remaining_bytes: int
    usage_percent: float
    state: str
    is_full: bool
    enforcement_enabled: bool

    def as_dict(self) -> dict[str, int | float | str | bool]:
        return asdict(self)


class PersistentStorageQuotaService:
    """The single authority for persistent per-user storage accounting."""

    @staticmethod
    def quota_state(*, used_bytes: int, quota_bytes: int) -> str:
        if used_bytes >= quota_bytes:
            return "full"
        usage_percent = (used_bytes / quota_bytes) * 100
        if usage_percent >= 95:
            return "near_limit"
        if usage_percent >= 80:
            return "approaching_limit"
        return "normal"

    @staticmethod
    def get_storage_quota(*, user) -> StorageQuotaSnapshot:
        quota_bytes = max(1, int(settings.PERSISTENT_STORAGE_QUOTA_BYTES))
        used_bytes = StorageArtifactLifecycleService.persistent_bytes_for_user(
            user_id=str(user.id)
        )
        usage_percent = round((used_bytes / quota_bytes) * 100, 2)
        is_full = used_bytes >= quota_bytes
        state = PersistentStorageQuotaService.quota_state(
            used_bytes=used_bytes,
            quota_bytes=quota_bytes,
        )
        return StorageQuotaSnapshot(
            used_bytes=used_bytes,
            quota_bytes=quota_bytes,
            remaining_bytes=max(0, quota_bytes - used_bytes),
            usage_percent=usage_percent,
            state=state,
            is_full=is_full,
            enforcement_enabled=bool(settings.PERSISTENT_STORAGE_QUOTA_ENFORCEMENT_ENABLED),
        )

    @classmethod
    def admission_is_blocked(cls, *, user) -> tuple[bool, StorageQuotaSnapshot]:
        snapshot = cls.get_storage_quota(user=user)
        return snapshot.enforcement_enabled and snapshot.is_full, snapshot

    @staticmethod
    def admin_storage_users_queryset(*, search: str = "", state: str = ""):
        """Return a compact, database-aggregated staff view of live user storage."""
        quota_bytes = max(1, int(settings.PERSISTENT_STORAGE_QUOTA_BYTES))
        persistent_artifacts = Q(
            stored_artifacts__storage_class="PERSISTENT",
            stored_artifacts__counts_toward_quota=True,
            stored_artifacts__deleted_at__isnull=True,
        )
        queryset = (
            get_user_model()
            .objects.annotate(
                persistent_bytes=Coalesce(
                    Sum("stored_artifacts__size_bytes", filter=persistent_artifacts),
                    Value(0),
                    output_field=BigIntegerField(),
                ),
                persistent_artifact_count=Count(
                    "stored_artifacts", filter=persistent_artifacts, distinct=True
                ),
            )
            .filter(persistent_bytes__gt=0)
        )
        if search:
            queryset = queryset.filter(
                Q(full_name__icontains=search)
                | Q(phone_number__icontains=search)
                | Q(email__icontains=search)
            )
        state_filters = {
            "approaching_limit": Q(
                persistent_bytes__gte=round(quota_bytes * 0.8),
                persistent_bytes__lt=round(quota_bytes * 0.95),
            ),
            "near_limit": Q(
                persistent_bytes__gte=round(quota_bytes * 0.95),
                persistent_bytes__lt=quota_bytes,
            ),
            "full": Q(persistent_bytes__gte=quota_bytes),
        }
        if state in state_filters:
            queryset = queryset.filter(state_filters[state])
        return queryset.order_by("-persistent_bytes", "-created_at")

    @classmethod
    def build_admin_storage_summary(cls) -> dict[str, int]:
        quota_bytes = max(1, int(settings.PERSISTENT_STORAGE_QUOTA_BYTES))
        users = cls.admin_storage_users_queryset()
        aggregate = StoredArtifact.objects.filter(
            storage_class="PERSISTENT",
            counts_toward_quota=True,
            deleted_at__isnull=True,
        ).aggregate(total_persistent_bytes=Coalesce(Sum("size_bytes"), Value(0)))
        return {
            "quota_bytes": quota_bytes,
            "total_persistent_bytes": int(aggregate["total_persistent_bytes"] or 0),
            "storage_user_count": users.count(),
            "approaching_limit_count": users.filter(
                persistent_bytes__gte=round(quota_bytes * 0.8),
                persistent_bytes__lt=round(quota_bytes * 0.95),
            ).count(),
            "near_limit_count": users.filter(
                persistent_bytes__gte=round(quota_bytes * 0.95),
                persistent_bytes__lt=quota_bytes,
            ).count(),
            "full_count": users.filter(persistent_bytes__gte=quota_bytes).count(),
        }

    @classmethod
    def get_admin_user_storage_detail(cls, *, user) -> dict:
        snapshot = cls.get_storage_quota(user=user)
        lectures = list(
            StoredArtifact.objects.filter(
                user=user,
                storage_class="PERSISTENT",
                counts_toward_quota=True,
                deleted_at__isnull=True,
                lecture__isnull=False,
            )
            .values("lecture_id", "lecture__title", "lecture__created_at")
            .annotate(
                persistent_bytes=Coalesce(Sum("size_bytes"), Value(0)),
                artifact_count=Count("id"),
            )
            .order_by("-persistent_bytes", "-lecture__created_at")
        )
        return {
            "storage": snapshot.as_dict(),
            "lectures": [
                {
                    "lecture_id": str(row["lecture_id"]),
                    "title": row["lecture__title"],
                    "created_at": row["lecture__created_at"],
                    "persistent_bytes": int(row["persistent_bytes"] or 0),
                    "artifact_count": int(row["artifact_count"] or 0),
                }
                for row in lectures
            ],
        }

    @classmethod
    def delete_lecture_and_artifacts(cls, *, user, lecture_id: str) -> StorageQuotaSnapshot:
        """Delete a completed lecture only after its registered objects are removed."""
        with transaction.atomic():
            locked_user = get_user_model().objects.select_for_update().get(id=user.id)
            lecture = Lecture.objects.select_for_update().filter(
                id=lecture_id, user=locked_user
            ).first()
            if lecture is None:
                raise Lecture.DoesNotExist
            if lecture.processing_jobs.filter(
                status__in=(
                    LectureProcessingJobStatus.PENDING,
                    LectureProcessingJobStatus.QUEUED,
                    LectureProcessingJobStatus.PROCESSING,
                )
            ).exists():
                raise StorageLectureActiveError

            artifacts = list(
                StoredArtifact.objects.select_for_update()
                .filter(user=locked_user, lecture=lecture, deleted_at__isnull=True)
                .order_by("created_at")
            )
            storage = get_storage_service()
            for artifact in artifacts:
                try:
                    storage.delete(artifact.storage_path)
                except StorageNotFoundError:
                    # A missing registered object is already absent from the bucket,
                    # so it is safe to complete deletion of its parent lecture.
                    continue
                except StorageError as exc:
                    raise StorageDeletionError from exc

            lecture.delete()
            return cls.get_storage_quota(user=locked_user)
