from __future__ import annotations

import logging
from datetime import datetime, time
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.admin_ops.models import VALUE_EVENT_NAMES, ProductEvent, ProductEventName

logger = logging.getLogger(__name__)


def start_of_value_day(moment: datetime | None = None) -> datetime:
    """Midnight of the current Tehran calendar day, the canonical analytics day."""
    local = timezone.localtime(moment or timezone.now(), settings.IRAN_BUSINESS_TIME_ZONE)
    return datetime.combine(local.date(), time.min, tzinfo=settings.IRAN_BUSINESS_TIME_ZONE)


class ProductAnalyticsService:
    """Records a constrained, privacy-preserving set of product milestones."""

    @staticmethod
    def record(
        *,
        name: str,
        user=None,
        visitor_id: str = "",
        session_id: str = "",
        path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> ProductEvent | None:
        if name not in ProductEventName.values:
            return None
        return ProductEvent.objects.create(
            name=name,
            user=user if getattr(user, "is_authenticated", False) else None,
            visitor_id=visitor_id[:64],
            session_id=session_id[:64],
            path=path[:240],
            metadata=metadata or {},
        )

    @staticmethod
    def record_first_note_ready(*, user) -> bool:
        """Record the user's first finished note, once per user, whatever promotions are on.

        The funnel's "first note ready" step used to be a side effect of opening
        the first-purchase discount window, so it vanished whenever that feature
        was off and skipped users who already had a window or a paid plan.
        """
        if not getattr(user, "is_authenticated", False):
            return False
        try:
            with transaction.atomic():
                if ProductEvent.objects.filter(user=user, name=ProductEventName.FIRST_NOTE_READY).exists():
                    return False
                ProductEvent.objects.create(name=ProductEventName.FIRST_NOTE_READY, user=user)
            return True
        except Exception:
            logger.warning("Could not record first note ready", exc_info=True)
            return False

    @staticmethod
    def record_value_event(*, name: str, user, note_id) -> bool:
        """Record that `user` used a study feature on one of their notes.

        Stored at most once per user/feature/note per Tehran day, so repeated
        clicks or polling cannot inflate the table or the dashboard. Never
        raises: analytics must not break a learner's request.
        """
        if name not in VALUE_EVENT_NAMES or not getattr(user, "is_authenticated", False):
            return False
        note_key = str(note_id)
        try:
            # Savepoint: a failure here must not poison a caller's transaction.
            with transaction.atomic():
                already_recorded = ProductEvent.objects.filter(
                    user=user,
                    name=name,
                    created_at__gte=start_of_value_day(),
                    metadata__note_id=note_key,
                ).exists()
                if already_recorded:
                    return False
                ProductEvent.objects.create(name=name, user=user, metadata={"note_id": note_key})
            return True
        except Exception:
            logger.warning("Could not record value event %s", name, exc_info=True)
            return False
