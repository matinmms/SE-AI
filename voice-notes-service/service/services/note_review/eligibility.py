"""Who gets the note accuracy check.

The feature is gated twice: a global capture switch (so nothing is requested or stored until
the founder turns it on) and a per-user flag (`User.accuracy_check_enabled`), unless
`NOTE_REVIEW_FOR_ALL` opens it to everyone.
"""

from __future__ import annotations

from django.conf import settings


def capture_enabled_for_user_id(user_id: object) -> bool:
    if not settings.NOTE_REVIEW_CAPTURE_ENABLED:
        return False
    if settings.NOTE_REVIEW_FOR_ALL:
        return True
    if not user_id:
        return False
    from apps.accounts.models import User

    try:
        return User.objects.filter(pk=str(user_id), accuracy_check_enabled=True).exists()
    except Exception:  # a malformed id must never break transcription
        return False


def review_visible_for(user) -> bool:
    """True when the viewer may see the accuracy check UI and API."""
    if not getattr(user, "is_authenticated", False):
        return False
    if not settings.NOTE_REVIEW_CAPTURE_ENABLED:
        return False
    return bool(settings.NOTE_REVIEW_FOR_ALL or getattr(user, "accuracy_check_enabled", False))


def capture_wanted_for_current_call(cost_context: dict | None = None) -> bool:
    """Used by the speech provider: is this request made for an eligible user?"""
    if not settings.NOTE_REVIEW_CAPTURE_ENABLED:
        return False
    if settings.NOTE_REVIEW_FOR_ALL:
        return True
    from apps.ai_costs.context import current_cost_context

    context = cost_context or current_cost_context()
    return capture_enabled_for_user_id(context.get("user_id"))
