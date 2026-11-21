"""Remembers that the object-storage bucket is full, so uploads fail fast instead of at the end.

A big upload used to travel for minutes and only then fail with a 500 when the server tried
to store it. Once any write is refused for quota, new uploads are refused at their first
chunk for a few minutes, and an expired-file cleanup is started to make room.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger("tesanote")

_FLAG_KEY = "object-storage:quota-exceeded"
_FLAG_SECONDS = 10 * 60


def storage_is_full() -> bool:
    try:
        return bool(cache.get(_FLAG_KEY))
    except Exception:  # an unreachable cache must not block uploads
        return False


def mark_storage_full() -> None:
    try:
        newly_marked = cache.add(_FLAG_KEY, 1, _FLAG_SECONDS)
    except Exception:
        newly_marked = False
    if not newly_marked:
        return
    logger.error("object_storage_quota_exceeded")
    try:
        from apps.storage_lifecycle.tasks import cleanup_expired_temporary_objects

        cleanup_expired_temporary_objects.delay()
    except Exception:
        logger.warning("quota_cleanup_not_scheduled", exc_info=True)


def clear_storage_full() -> None:
    try:
        cache.delete(_FLAG_KEY)
    except Exception:
        pass
