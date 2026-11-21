"""Emergency mode: the international internet is down and only Iran-hosted AI is available.

Everything here is fail-safe in one direction: if the state cannot be read for any reason, the
site behaves exactly as it does today (normal mode). Emergency mode only ever turns on through an
explicit admin action.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("tesanote")

CACHE_KEY = "system-mode:emergency"
CACHE_SECONDS = 5
CONFIRM_ON = "فعال‌سازی اضطرار"
CONFIRM_OFF = "پایان اضطرار"
EMERGENCY_MESSAGE = (
    "به دلیل قطع اینترنت بین‌المللی، سرویس تبدیل صوت به جزوه فعلاً در دسترس نیست. "
    "ساخت جزوه از روی متن و استفاده از جزوه‌های قبلی همچنان فعال است."
)


def is_emergency() -> bool:
    """True only while an admin has switched emergency mode on. Any error means False."""
    try:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return bool(cached)
    except Exception:  # noqa: BLE001 - an unreachable cache must not change behaviour
        cached = None
    try:
        from apps.admin_ops.models import SystemMode

        active = bool(SystemMode.objects.filter(key="primary", emergency_active=True).exists())
    except Exception:  # noqa: BLE001 - database trouble also means "normal"
        logger.warning("system_mode_read_failed", exc_info=True)
        return False
    try:
        cache.set(CACHE_KEY, active, CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        pass
    return active


def _forget_cache() -> None:
    try:
        cache.delete(CACHE_KEY)
    except Exception:  # noqa: BLE001
        pass


def status_payload() -> dict[str, object]:
    from apps.admin_ops.models import SystemMode

    mode = SystemMode.objects.select_related("activated_by").filter(key="primary").first()
    return {
        "emergency": bool(mode and mode.emergency_active),
        "activated_at": mode.activated_at if mode and mode.emergency_active else None,
        "activated_by": (
            (mode.activated_by.full_name or mode.activated_by.phone_number or "") if mode and mode.activated_by else ""
        ),
        "reason": mode.reason if mode and mode.emergency_active else "",
    }


def ensure_voice_pipeline_available() -> None:
    """Refuse a request that needs speech-to-text while emergency mode is on."""
    if not is_emergency():
        return
    from apps.uploads.exceptions import UploadAPIException

    raise UploadAPIException(message=EMERGENCY_MESSAGE, error_code="EMERGENCY_MODE", status_code=503)


@transaction.atomic
def set_emergency(*, active: bool, actor, reason: str = "") -> dict[str, object]:
    """Turn emergency mode on or off (idempotent). Turning it off gives paid plans their time back."""
    from apps.admin_ops.models import AdminAuditAction, EmergencyPeriod, SystemMode
    from services.admin.audit import AdminAuditService

    mode, _ = SystemMode.objects.select_for_update().get_or_create(key="primary")
    now = timezone.now()
    changed = mode.emergency_active != active
    extended = 0
    if changed and active:
        mode.emergency_active = True
        mode.activated_at = now
        mode.activated_by = actor
        mode.reason = reason[:255]
        mode.save()
        EmergencyPeriod.objects.create(started_at=now, started_by=actor, reason=reason[:255])
    elif changed:
        mode.emergency_active = False
        mode.save(update_fields=["emergency_active", "updated_at"])
        period = EmergencyPeriod.objects.select_for_update().filter(ended_at__isnull=True).first()
        if period is not None:
            period.ended_at = now
            period.ended_by = actor
            period.save(update_fields=["ended_at", "ended_by", "updated_at"])
            extended = extend_paid_plans_for_period(period=period)
    if changed:
        AdminAuditService.record(
            action=AdminAuditAction.EMERGENCY_MODE_CHANGED,
            actor=actor,
            target_user=None,
            entity_type="system_mode",
            entity_id="primary",
            metadata={"active": active, "reason": reason[:255], "plans_extended": extended},
        )
        _forget_cache()
        transaction.on_commit(_forget_cache)  # and again once the change is visible to other connections
        logger.warning("emergency_mode_changed", extra={"active": active, "plans_extended": extended})
    return {**status_payload(), "changed": changed, "plans_extended": extended}


def extend_paid_plans_for_period(*, period) -> int:
    """Add the length of an emergency period to every active paid plan that was running during it.

    The "your credit is not lost" promise: paid users do not lose plan time while voice-to-note is
    unavailable. Runs once per period (stamped), so a retry never extends a plan twice.
    """
    from apps.subscriptions.models import PlanKey, UserSubscription

    if period.plans_extended_at is not None or period.ended_at is None:
        return int(period.plans_extended_count or 0)
    length = period.ended_at - period.started_at
    if length <= timedelta(0):
        period.plans_extended_at = timezone.now()
        period.save(update_fields=["plans_extended_at", "updated_at"])
        return 0
    affected = (
        UserSubscription.objects.select_for_update()
        .filter(is_active=True, started_at__lt=period.ended_at, expires_at__gt=period.started_at)
        .exclude(plan__key=PlanKey.FREE)
    )
    count = 0
    for subscription in affected:
        subscription.expires_at = subscription.expires_at + length
        subscription.save(update_fields=["expires_at", "updated_at"])
        count += 1
    period.plans_extended_at = timezone.now()
    period.plans_extended_count = count
    period.save(update_fields=["plans_extended_at", "plans_extended_count", "updated_at"])
    return count


def emergency_credit(*, started_at, ends_at) -> timedelta:
    """Total length of finished emergency periods that overlapped a plan's life [started_at, ends_at].

    The daily entitlement check derives a plan's true end from its payment; the time given back
    for emergency periods must count as part of that entitlement or the check would undo it.
    """
    from apps.admin_ops.models import EmergencyPeriod

    total = timedelta(0)
    try:
        periods = EmergencyPeriod.objects.filter(
            ended_at__isnull=False, started_at__lt=ends_at, ended_at__gt=started_at
        )
        for period in periods:
            total += period.ended_at - period.started_at
    except Exception:  # noqa: BLE001 - no credit is the conservative answer
        logger.warning("emergency_credit_failed", exc_info=True)
    return total
