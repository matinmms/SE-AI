"""Delete a spot's stored audio clip whenever the spot row goes (note, lecture or account deletion)."""

from __future__ import annotations

import logging

from django.db.models.signals import post_delete
from django.dispatch import receiver

from apps.notes.models import NoteReviewSpot

logger = logging.getLogger("tesanote")


@receiver(post_delete, sender=NoteReviewSpot)
def delete_clip_with_spot(sender, instance: NoteReviewSpot, **kwargs) -> None:
    if not instance.clip_path:
        return
    try:
        from apps.storage_lifecycle.services import StorageArtifactLifecycleService
        from infrastructure.storage.factory import get_storage_service

        get_storage_service().delete(instance.clip_path)
        StorageArtifactLifecycleService.mark_deleted(storage_path=instance.clip_path)
    except Exception:
        logger.exception("note_review_clip_cleanup_failed", extra={"storage_path": instance.clip_path})
