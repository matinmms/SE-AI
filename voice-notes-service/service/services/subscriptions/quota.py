from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from django.db import transaction

from apps.subscriptions.models import UserSubscription
from services.subscriptions.lifecycle import SubscriptionLifecycleService

logger = logging.getLogger("tesanote")


@dataclass(frozen=True)
class ReservationDecision:
    allowed: bool
    required_minutes: int
    available_minutes: int


class SubscriptionQuotaService:
    @staticmethod
    def minutes_required_for_duration(*, duration_seconds: int) -> int:
        return max(1, math.ceil(max(0, duration_seconds) / 60))

    @staticmethod
    def can_reserve_minutes(*, subscription: UserSubscription, minutes: int) -> ReservationDecision:
        return ReservationDecision(
            allowed=subscription.available_minutes >= minutes,
            required_minutes=minutes,
            available_minutes=subscription.available_minutes,
        )

    @staticmethod
    @transaction.atomic
    def reserve_minutes(*, user, minutes: int, lecture=None, enforce_available: bool = True) -> UserSubscription:
        subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .get(user=user, is_active=True)
        )
        decision = SubscriptionQuotaService.can_reserve_minutes(subscription=subscription, minutes=minutes)
        if enforce_available and not decision.allowed:
            raise ValueError("INSUFFICIENT_AVAILABLE_MINUTES")
        subscription.reserved_minutes += minutes
        subscription.save(update_fields=["reserved_minutes", "updated_at"])
        SubscriptionLifecycleService.sync_legacy_user_fields(user=user, subscription=subscription)
        logger.info(
            "quota_reserved",
            extra={
                "user_id": str(user.id),
                "lecture_id": str(lecture.id) if lecture else "",
                "subscription_id": str(subscription.id),
                "minutes": minutes,
                "remaining_minutes": subscription.remaining_minutes,
                "reserved_minutes": subscription.reserved_minutes,
            },
        )
        return subscription

    @staticmethod
    @transaction.atomic
    def release_reserved_minutes(*, user, minutes: int, lecture=None) -> UserSubscription:
        subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .get(user=user, is_active=True)
        )
        subscription.reserved_minutes = max(0, subscription.reserved_minutes - minutes)
        subscription.save(update_fields=["reserved_minutes", "updated_at"])
        SubscriptionLifecycleService.sync_legacy_user_fields(user=user, subscription=subscription)
        logger.info(
            "quota_released",
            extra={
                "user_id": str(user.id),
                "lecture_id": str(lecture.id) if lecture else "",
                "subscription_id": str(subscription.id),
                "minutes": minutes,
                "remaining_minutes": subscription.remaining_minutes,
                "reserved_minutes": subscription.reserved_minutes,
            },
        )
        return subscription

    @staticmethod
    @transaction.atomic
    def finalize_reserved_minutes(*, user, minutes: int, lecture=None) -> UserSubscription:
        subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .get(user=user, is_active=True)
        )
        finalized_minutes = min(minutes, subscription.reserved_minutes)
        subscription.reserved_minutes = max(0, subscription.reserved_minutes - finalized_minutes)
        subscription.remaining_minutes = max(0, subscription.remaining_minutes - finalized_minutes)
        subscription.used_minutes += finalized_minutes
        subscription.save(update_fields=["reserved_minutes", "remaining_minutes", "used_minutes", "updated_at"])
        SubscriptionLifecycleService.sync_legacy_user_fields(user=user, subscription=subscription)
        logger.info(
            "quota_finalized",
            extra={
                "user_id": str(user.id),
                "lecture_id": str(lecture.id) if lecture else "",
                "subscription_id": str(subscription.id),
                "minutes": finalized_minutes,
                "remaining_minutes": subscription.remaining_minutes,
                "reserved_minutes": subscription.reserved_minutes,
                "used_minutes": subscription.used_minutes,
            },
        )
        return subscription

    @staticmethod
    @transaction.atomic
    def finalize_actual_minutes(
        *, user, reserved_minutes: int, actual_minutes: int, lecture=None
    ) -> UserSubscription:
        """Clear the original provisional reservation and deduct the real,
        pipeline-measured minutes actually consumed.

        `reserved_minutes` and `actual_minutes` are deliberately independent:
        the reservation is only ever an admission-control hold made from an
        upload-time duration estimate, and that estimate can be wrong (e.g.
        a metadata-extraction failure). This always deducts the true amount
        from `remaining_minutes`/`used_minutes` -- capping consumption at
        whatever was reserved would silently under-bill exactly the cases
        this exists to fix.
        """
        subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .get(user=user, is_active=True)
        )
        subscription.reserved_minutes = max(0, subscription.reserved_minutes - reserved_minutes)
        # Included minutes are drawn down first; only the overflow beyond
        # them touches purchased bonus/top-up minutes.
        included_consumed = min(actual_minutes, subscription.remaining_minutes)
        bonus_consumed = actual_minutes - included_consumed
        subscription.remaining_minutes = max(0, subscription.remaining_minutes - included_consumed)
        subscription.bonus_minutes = max(0, subscription.bonus_minutes - bonus_consumed)
        subscription.used_minutes += actual_minutes
        subscription.save(
            update_fields=[
                "reserved_minutes",
                "remaining_minutes",
                "bonus_minutes",
                "used_minutes",
                "updated_at",
            ]
        )
        SubscriptionLifecycleService.sync_legacy_user_fields(user=user, subscription=subscription)
        logger.info(
            "quota_finalized_actual",
            extra={
                "user_id": str(user.id),
                "lecture_id": str(lecture.id) if lecture else "",
                "subscription_id": str(subscription.id),
                "reserved_minutes": reserved_minutes,
                "actual_minutes": actual_minutes,
                "included_consumed": included_consumed,
                "bonus_consumed": bonus_consumed,
                "remaining_minutes": subscription.remaining_minutes,
                "bonus_minutes": subscription.bonus_minutes,
                "used_minutes": subscription.used_minutes,
            },
        )
        return subscription

