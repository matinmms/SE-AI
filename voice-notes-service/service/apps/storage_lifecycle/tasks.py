import logging
import time
from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.utils import timezone

from apps.storage_lifecycle.services import StorageArtifactLifecycleService

# One run keeps going batch after batch until nothing is left or this budget is spent.
# A single batch per day could not keep up (hundreds of chunk objects per lecture), so the
# backlog grew until the bucket's quota was full and uploads started failing.
DRAIN_BUDGET_SECONDS = 8 * 60
DRAIN_BATCH_SIZE = 500
PURGE_BUDGET_SECONDS = 6 * 60

logger = logging.getLogger("tesanote.storage_lifecycle")


@shared_task(ignore_result=True)
def cleanup_expired_temporary_objects() -> dict[str, int]:
    totals: dict[str, int] = {}
    deadline = time.monotonic() + DRAIN_BUDGET_SECONDS
    while True:
        result = StorageArtifactLifecycleService.cleanup_expired_temporary_objects(
            batch_size=DRAIN_BATCH_SIZE
        )
        for key, value in result.items():
            totals[key] = totals.get(key, 0) + int(value)
        progressed = int(result.get("deleted", 0)) + int(result.get("missing", 0))
        if result.get("disabled") or not result.get("eligible") or not progressed:
            break
        if result.get("throttled"):
            break  # the provider asked us to slow down; the next scheduled run continues
        if time.monotonic() >= deadline:
            break
    totals.update(purge_deleted_temporary_versions())
    return totals


def purge_deleted_temporary_versions() -> dict[str, int]:
    """Free the space deleted temporary files still hold in the versioned bucket.

    The bucket keeps every deleted file as a hidden old version, so deleting alone frees
    nothing. Only the temporary/ hierarchy is touched, and only old versions and delete
    markers: a file that still exists keeps its current version.
    """
    from infrastructure.storage.factory import get_storage_service

    if not settings.TEMP_STORAGE_CLEANUP_ENABLED:
        return {}
    storage = get_storage_service()
    if not hasattr(storage, "purge_noncurrent_versions"):
        return {}
    # A delete becomes permanent after a short grace period, like the lifecycle rule intended.
    older_than = timezone.now() - timedelta(hours=1)
    try:
        result = storage.purge_noncurrent_versions(
            relative_prefix="temporary", older_than=older_than, max_seconds=PURGE_BUDGET_SECONDS
        )
    except Exception:
        logger.warning("temporary_version_purge_failed", exc_info=True)
        return {"purge_failed": 1}
    logger.info("temporary_version_purge_complete", extra=result)
    return {f"purged_{key}": int(value) for key, value in result.items()}
