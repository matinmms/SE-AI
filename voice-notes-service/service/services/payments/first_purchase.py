from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.payments.models import (
    FirstPurchaseDiscount,
    PaymentPurpose,
    PaymentRecord,
    PaymentStatus,
)
from apps.subscriptions.models import PlanKey, SubscriptionPlan

logger = logging.getLogger("tesanote")


def percent_amount(base: int, percent: int) -> int:
    """Integer discount, rounded exactly like the public plan discount."""
    if base <= 0 or percent <= 0:
        return 0
    return min(base, int(round((base * percent) / 100)))


class FirstPurchaseDiscountService:
    @staticmethod
    def is_enabled() -> bool:
        return bool(settings.FIRST_PURCHASE_DISCOUNT_ENABLED) and settings.FIRST_PURCHASE_DISCOUNT_PERCENT > 0

    @staticmethod
    def has_paid_plan(user) -> bool:
        return PaymentRecord.objects.filter(
            user=user,
            purpose=PaymentPurpose.PLAN_PURCHASE,
            status=PaymentStatus.SUCCESS,
        ).exists()

    @staticmethod
    def open_window(*, user, now=None, first_note_ready: bool = False) -> FirstPurchaseDiscount | None:
        """Create the user's single discount window if they never had one.

        Idempotent and race-safe (unique per user): a second call returns the
        existing row untouched, so the expiry can never be pushed forward.
        """
        if not FirstPurchaseDiscountService.is_enabled():
            return None
        existing = FirstPurchaseDiscount.objects.filter(user=user).first()
        if existing is not None:
            return existing
        if FirstPurchaseDiscountService.has_paid_plan(user):
            return None
        now = now or timezone.now()
        try:
            with transaction.atomic():
                window = FirstPurchaseDiscount.objects.create(
                    user=user,
                    percent=min(100, settings.FIRST_PURCHASE_DISCOUNT_PERCENT),
                    starts_at=now,
                    expires_at=now + timedelta(hours=settings.FIRST_PURCHASE_DISCOUNT_HOURS),
                )
        except IntegrityError:
            return FirstPurchaseDiscount.objects.filter(user=user).first()
        if first_note_ready:
            from services.analytics.product import ProductAnalyticsService

            ProductAnalyticsService.record_first_note_ready(user=user)
        return window

    @staticmethod
    def get_window(*, user, now=None) -> FirstPurchaseDiscount | None:
        """Existing window, or a lazily opened one for an existing user who already has a ready note."""
        if not getattr(user, "is_authenticated", False):
            return None
        window = FirstPurchaseDiscount.objects.filter(user=user).first()
        if window is not None:
            return window
        if not FirstPurchaseDiscountService.is_enabled():
            return None
        from apps.lectures.models import LectureProcessingState

        if not user.lectures.filter(processing_state=LectureProcessingState.COMPLETED).exists():
            return None
        return FirstPurchaseDiscountService.open_window(user=user, now=now)

    @staticmethod
    def active_percent(*, user, now=None) -> int:
        """Percent that applies to a plan purchase right now; 0 when none (never negative, never expired)."""
        if not FirstPurchaseDiscountService.is_enabled():
            return 0
        now = now or timezone.now()
        window = FirstPurchaseDiscountService.get_window(user=user, now=now)
        if window is None or not (window.starts_at <= now < window.expires_at):
            return 0
        if FirstPurchaseDiscountService.has_paid_plan(user):
            return 0
        return window.percent

    @staticmethod
    def state_for(*, user, now=None) -> dict[str, object]:
        now = now or timezone.now()
        window = FirstPurchaseDiscountService.get_window(user=user, now=now)
        percent = FirstPurchaseDiscountService.active_percent(user=user, now=now)
        expired = window is not None and now >= window.expires_at
        if expired and window.expiry_recorded_at is None:
            FirstPurchaseDiscountService.record_expiry(window, now=now)
        plan_prices: dict[str, dict[str, int]] = {}
        if percent > 0:
            from services.payments.manager import PaymentManagerService

            for plan in SubscriptionPlan.objects.filter(is_active=True).exclude(key=PlanKey.FREE):
                before = PaymentManagerService.calculate_plan_pricing(plan=plan, discount=None, now=now)
                after = PaymentManagerService.calculate_plan_pricing(
                    plan=plan, discount=None, now=now, first_purchase_percent=percent
                )
                plan_prices[plan.key] = {
                    "price_before": int(before["amount"]),
                    "price_after": int(after["amount"]),
                }
        return {
            "eligible": percent > 0,
            "percent": percent,
            "starts_at": window.starts_at if window else None,
            "expires_at": window.expires_at if window else None,
            "server_now": now,
            "seconds_remaining": max(0, int((window.expires_at - now).total_seconds())) if window and percent else 0,
            "plan_prices": plan_prices,
        }

    @staticmethod
    def record_expiry(window: FirstPurchaseDiscount, *, now=None) -> bool:
        """Mark expiry once; emits the DISCOUNT_EXPIRED event only for users who never bought."""
        now = now or timezone.now()
        updated = FirstPurchaseDiscount.objects.filter(
            pk=window.pk, expiry_recorded_at__isnull=True, expires_at__lte=now
        ).update(expiry_recorded_at=now)
        if not updated:
            return False
        window.expiry_recorded_at = now
        if not FirstPurchaseDiscountService.has_paid_plan(window.user):
            from services.analytics.product import ProductAnalyticsService

            ProductAnalyticsService.record(
                name="DISCOUNT_EXPIRED",
                user=window.user,
                metadata={"percent": window.percent},
            )
        return True

    @staticmethod
    def record_expired_windows(*, now=None, limit: int = 500) -> int:
        now = now or timezone.now()
        count = 0
        for window in FirstPurchaseDiscount.objects.filter(
            expiry_recorded_at__isnull=True, expires_at__lte=now
        ).select_related("user")[:limit]:
            if FirstPurchaseDiscountService.record_expiry(window, now=now):
                count += 1
        return count
