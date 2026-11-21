"""Note deletion: hidden at once, removed from the database and storage one day later.

Notes are never deleted by anything except the user's own delete action. That action only sets
`deleted_at` (and revokes the share link, frees the storage quota); `purge_deleted_notes` then removes
every note whose `deleted_at` is at least NOTE_PURGE_AFTER old, with its files.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from apps.notes.models import GeneratedNote, GeneratedNoteSummary, NotePdf
from apps.referrals.models import NoteShareLink
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from infrastructure.storage.base import StorageError, StorageNotFoundError
from infrastructure.storage.factory import get_storage_service

logger = logging.getLogger("tesanote")

NOTE_PURGE_AFTER = timedelta(days=1)


def note_storage_paths(note: GeneratedNote) -> set[str]:
    paths = {note.html_path, note.pdf_path}
    try:
        record = note.pdf_record
    except NotePdf.DoesNotExist:
        record = None
    if record:
        paths.add(record.storage_path)
    summary = getattr(note, "study_summary", None)
    if summary:
        paths.update((summary.html_path, summary.pdf_path))
    paths.discard("")
    return paths


def _used_by_another_note(note: GeneratedNote, storage_path: str) -> bool:
    """Another note (even one waiting for its purge) or summary still points at the same file."""
    return (
        GeneratedNote.all_objects.exclude(id=note.id).filter(Q(html_path=storage_path) | Q(pdf_path=storage_path)).exists()
        or GeneratedNoteSummary.objects.exclude(source_note=note).filter(Q(html_path=storage_path) | Q(pdf_path=storage_path)).exists()
        or NotePdf.objects.exclude(note=note).filter(storage_path=storage_path).exists()
    )


def soft_delete_note(note: GeneratedNote, *, now=None) -> None:
    """The user deleted the note: hide it everywhere now, remove it for good after NOTE_PURGE_AFTER."""
    now = now or timezone.now()
    note.deleted_at = now
    note.save(update_fields=["deleted_at", "updated_at"])
    NoteShareLink.objects.filter(note=note, revoked_at__isnull=True).update(revoked_at=now)
    for storage_path in note_storage_paths(note):
        if not GeneratedNote.objects.exclude(id=note.id).filter(Q(html_path=storage_path) | Q(pdf_path=storage_path)).exists():
            # Frees the user's storage quota now; the files themselves go at purge time.
            StorageArtifactLifecycleService.mark_deleted(storage_path=storage_path)


def purge_note(note: GeneratedNote) -> None:
    """Delete the note's files and then the note itself. Raises StorageError if a file could not be removed."""
    storage = get_storage_service()
    for storage_path in note_storage_paths(note):
        if _used_by_another_note(note, storage_path):
            continue
        try:
            storage.delete(storage_path)
        except StorageNotFoundError:
            pass
        StorageArtifactLifecycleService.mark_deleted(storage_path=storage_path)
    note.delete()


def purge_deleted_notes(*, now=None) -> dict[str, int]:
    """Remove every note deleted at least NOTE_PURGE_AFTER ago. A failed one is retried on the next run."""
    now = now or timezone.now()
    result = {"purged": 0, "failed": 0}
    for note in GeneratedNote.all_objects.filter(deleted_at__isnull=False, deleted_at__lte=now - NOTE_PURGE_AFTER).iterator():
        try:
            purge_note(note)
        except StorageError:
            logger.warning("note_purge_storage_failed", extra={"note_id": str(note.id)}, exc_info=True)
            result["failed"] += 1
        else:
            result["purged"] += 1
    return result
