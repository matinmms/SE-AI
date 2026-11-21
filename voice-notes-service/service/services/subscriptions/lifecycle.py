from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.accounts.deleted_numbers import phone_was_deleted
from apps.admin_ops.system_mode import emergency_credit, is_emergency
from apps.accounts.models import SubscriptionPlan as LegacySubscriptionPlan
from apps.subscriptions.models import PlanKey, SubscriptionPlan, UserSubscription

logger = logging.getLogger("tesanote")

# A payment and the subscription it activates are timestamped moments apart;
# only extensions beyond this (the old sweep added whole months) are wrong.
ENTITLEMENT_GRACE = timedelta(days=1)

_LEGACY_PLAN_MAP = {
    PlanKey.FREE: LegacySubscriptionPlan.FREE,
    PlanKey.PLUS: LegacySubscriptionPlan.PRO,
    PlanKey.PRO: LegacySubscriptionPlan.PREMIUM,
    PlanKey.MAX: LegacySubscriptionPlan.MAX,
}


class SubscriptionLifecycleService:
    @staticmethod
    def get_plan(*, key: str) -> SubscriptionPlan:
        return SubscriptionPlan.objects.get(key=key)

    @staticmethod
    def get_active_subscription(*, user) -> UserSubscription:
        subscription = (
            UserSubscription.objects.select_related("plan")
            .filter(user=user, is_active=True)
            .order_by("-created_at")
            .first()
        )
        if subscription is None:
            subscription = SubscriptionLifecycleService.create_subscription(user=user, plan_key=PlanKey.FREE)
        return subscription

    @staticmethod
    @transaction.atomic
    def create_subscription(
        *,
        user,
        plan_key: str,
        started_at=None,
        expires_at=None,
        carry_over_paid_minutes: bool = False,
        carry_over_remaining_time: bool = False,
    ) -> UserSubscription:
        plan = SubscriptionLifecycleService.get_plan(key=plan_key)
        now = started_at or timezone.now()
        end_at = expires_at or plan.expiry_from(started_at=now)
        active_subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan")
            .filter(user=user, is_active=True)
            .order_by("-created_at")
            .first()
        )
        carry_over_minutes = 0
        carry_over_bonus = 0
        carry_over_reserved = 0
        if carry_over_paid_minutes and active_subscription is not None and plan.key != PlanKey.FREE:
            # Whatever the user still has -- including a Free plan's unused
            # minutes -- is added on top of the new plan's allowance instead
            # of being thrown away when they buy. Reservations of uploads
            # still processing move with it so they settle against the new row.
            carry_over_minutes = max(0, active_subscription.remaining_minutes)
            carry_over_bonus = max(0, active_subscription.bonus_minutes)
            carry_over_reserved = max(0, active_subscription.reserved_minutes)

        carried_over_time = timedelta(0)
        if (
            expires_at is None
            and carry_over_remaining_time
            and active_subscription is not None
            and active_subscription.plan.key != PlanKey.FREE
            and plan.key != PlanKey.FREE
        ):
            # A user buying a new plan while their current paid plan still
            # has time left on it must not lose that unused time -- it
            # extends the new plan's expiry instead of being discarded.
            remaining_time = active_subscription.expires_at - now
            if remaining_time > timedelta(0):
                carried_over_time = remaining_time
                end_at = end_at + remaining_time

        # A number whose account was deleted gets its first Free cycle with no minutes: the free
        # allowance comes back with the next monthly cycle (or they buy a plan), not on re-signup.
        withhold_free_minutes = (
            plan.key == PlanKey.FREE
            and not UserSubscription.objects.filter(user=user).exists()
            and phone_was_deleted(getattr(user, "phone_number", None))
        )
        if withhold_free_minutes:
            logger.info("free_minutes_withheld_for_returning_number", extra={"user_id": str(user.id)})

        UserSubscription.objects.filter(user=user, is_active=True).update(is_active=False, updated_at=timezone.now())
        subscription = UserSubscription.objects.create(
            user=user,
            plan=plan,
            started_at=now,
            expires_at=end_at,
            remaining_minutes=(0 if withhold_free_minutes else plan.monthly_minutes) + carry_over_minutes,
            bonus_minutes=carry_over_bonus,
            reserved_minutes=carry_over_reserved,
            used_minutes=0,
            is_active=True,
        )
        SubscriptionLifecycleService.sync_legacy_user_fields(user=user, subscription=subscription)
        logger.info(
            "subscription_activated",
            extra={
                "user_id": str(user.id),
                "subscription_id": str(subscription.id),
                "plan_key": plan.key,
                "carry_over_minutes": carry_over_minutes,
                "carry_over_bonus_minutes": carry_over_bonus,
                "carried_over_time_seconds": int(carried_over_time.total_seconds()),
            },
        )
        return subscription

    @staticmethod
    @transaction.atomic
    def activate_subscription(*, subscription: UserSubscription) -> UserSubscription:
        UserSubscription.objects.filter(user=subscription.user, is_active=True).exclude(id=subscription.id).update(
            is_active=False,
            updated_at=timezone.now(),
        )
        subscription.is_active = True
        subscription.save(update_fields=["is_active", "updated_at"])
        SubscriptionLifecycleService.sync_legacy_user_fields(user=subscription.user, subscription=subscription)
        logger.info(
            "subscription_activated",
            extra={"user_id": str(subscription.user_id), "subscription_id": str(subscription.id), "plan_key": subscription.plan.key},
        )
        return subscription

    @staticmethod
    @transaction.atomic
    def renew_subscription(*, subscription: UserSubscription, renewal_start=None) -> UserSubscription:
        """Refresh a subscription for another term of its own plan, in
        place. Used for the Free plan's monthly refresh; paid plans use
        `downgrade_to_free` instead once their term ends (see
        `reset_due_subscriptions`) rather than silently re-granting a paid
        allowance without a new payment.
        """
        start_at = renewal_start or max(subscription.expires_at, timezone.now())
        subscription.started_at = start_at
        subscription.expires_at = subscription.plan.expiry_from(started_at=start_at)
        subscription.remaining_minutes = subscription.plan.monthly_minutes
        subscription.reserved_minutes = 0
        subscription.used_minutes = 0
        subscription.bonus_minutes = 0
        subscription.is_active = True
        subscription.save(
            update_fields=[
                "started_at",
                "expires_at",
                "remaining_minutes",
                "reserved_minutes",
                "used_minutes",
                "bonus_minutes",
                "is_active",
                "updated_at",
            ]
        )
        SubscriptionLifecycleService.sync_legacy_user_fields(user=subscription.user, subscription=subscription)
        logger.info(
            "subscription_reset",
            extra={"user_id": str(subscription.user_id), "subscription_id": str(subscription.id), "plan_key": subscription.plan.key},
        )
        return subscription

    @staticmethod
    @transaction.atomic
    def downgrade_to_free(*, subscription: UserSubscription, downgrade_at=None) -> UserSubscription:
        """A paid plan's term ended without a renewal payment -- move the
        user to a fresh Free-plan cycle rather than re-granting the paid
        allowance indefinitely. Any remaining included or bonus minutes on
        the expired plan are deliberately not carried over; they belonged
        to the term that just ended.
        """
        now = downgrade_at or timezone.now()
        in_flight_reserved = subscription.reserved_minutes
        new_subscription = SubscriptionLifecycleService.create_subscription(
            user=subscription.user,
            plan_key=PlanKey.FREE,
            started_at=now,
        )
        if in_flight_reserved:
            # Quota settlement always targets the active subscription, so an
            # upload still processing must keep its hold on the new one;
            # otherwise the Free allowance is reservable twice.
            new_subscription.reserved_minutes = in_flight_reserved
            new_subscription.save(update_fields=["reserved_minutes", "updated_at"])
        logger.info(
            "subscription_downgraded_to_free",
            extra={
                "user_id": str(subscription.user_id),
                "expired_subscription_id": str(subscription.id),
                "expired_plan_key": subscription.plan.key,
                "new_subscription_id": str(new_subscription.id),
            },
        )
        return new_subscription

    @staticmethod
    def paid_entitlement_ends(*, user_ids) -> dict:
        """When each user's paid access truly ends: the latest end among
        their successful plan purchases (payment time + that plan's
        duration). Every paid activation path records such a payment, so a
        user absent from the result has never paid for their paid plan.
        """
        from apps.payments.models import PaymentPurpose, PaymentRecord, PaymentStatus

        ends: dict = {}
        payments = (
            PaymentRecord.objects.filter(
                user_id__in=list(user_ids),
                status=PaymentStatus.SUCCESS,
                purpose=PaymentPurpose.PLAN_PURCHASE,
            )
            .select_related("subscription_plan")
            .only("user_id", "paid_at", "updated_at", "subscription_plan__duration_months")
        )
        for payment in payments:
            paid_at = payment.paid_at or payment.updated_at
            end = payment.subscription_plan.expiry_from(started_at=paid_at)
            if payment.user_id not in ends or end > ends[payment.user_id]:
                ends[payment.user_id] = end
        return ends

    @staticmethod
    @transaction.atomic
    def enforce_paid_entitlements(*, now=None, apply: bool = True) -> list[dict]:
        """Cap every active paid plan at what its payments actually bought.

        Before the plan redesign, the expiry sweep renewed paid plans in
        place without a payment, pushing `expires_at` forward every month.
        Those rows never look "due", so this derives the real end from
        payments: past it -> downgrade to Free; still ahead but the row runs
        longer -> shorten `expires_at` to it. Staff accounts are excluded.
        With `apply=False` nothing is written (a dry run for review).
        """
        current_time = now or timezone.now()
        subscriptions = list(
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .filter(is_active=True)
            .exclude(plan__key=PlanKey.FREE)
            .exclude(user__is_staff=True)
            .exclude(granted_by_admin=True)
            .order_by("expires_at")
        )
        ends = SubscriptionLifecycleService.paid_entitlement_ends(
            user_ids={subscription.user_id for subscription in subscriptions}
        )
        actions: list[dict] = []
        emergency_now = is_emergency()
        for subscription in subscriptions:
            if emergency_now:
                continue  # paid plans are frozen while emergency mode is on
            entitlement_end = ends.get(subscription.user_id)
            if entitlement_end is not None:
                # Plan time given back for emergency periods is part of what the payment bought.
                entitlement_end += emergency_credit(started_at=subscription.started_at, ends_at=subscription.expires_at)
            if entitlement_end is None or entitlement_end <= current_time:
                action = "downgrade"
            elif subscription.expires_at > entitlement_end + ENTITLEMENT_GRACE:
                action = "shorten"
            else:
                continue
            actions.append(
                {
                    "user_id": str(subscription.user_id),
                    "contact": subscription.user.phone_number or subscription.user.email or "",
                    "plan_key": subscription.plan.key,
                    "action": action,
                    "entitlement_end": entitlement_end,
                    "previous_expires_at": subscription.expires_at,
                }
            )
            if not apply:
                continue
            if action == "downgrade":
                SubscriptionLifecycleService.downgrade_to_free(
                    subscription=subscription, downgrade_at=current_time
                )
            else:
                subscription.expires_at = entitlement_end
                subscription.save(update_fields=["expires_at", "updated_at"])
            logger.info(
                "paid_entitlement_enforced",
                extra={
                    "user_id": str(subscription.user_id),
                    "subscription_id": str(subscription.id),
                    "plan_key": subscription.plan.key,
                    "action": action,
                },
            )
        return actions

    @staticmethod
    @transaction.atomic
    def reset_due_subscriptions(*, now=None) -> int:
        """Staff accounts are excluded: an admin's own plan (often granted
        manually for testing) must never be auto-downgraded or auto-renewed
        by this job. Paid plans are first capped to what their payments
        bought, so a wrongly extended row can never outlive its payment.
        """
        current_time = now or timezone.now()
        processed_count = sum(
            1
            for action in SubscriptionLifecycleService.enforce_paid_entitlements(now=current_time)
            if action["action"] == "downgrade"
        )
        due_subscriptions = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .filter(is_active=True, expires_at__lte=current_time)
            .exclude(user__is_staff=True)
            .order_by("expires_at")
        )
        emergency_now = is_emergency()
        for subscription in due_subscriptions:
            if emergency_now and subscription.plan.key != PlanKey.FREE:
                continue  # paid plans do not expire during emergency mode; their time is given back
            if subscription.plan.key == PlanKey.FREE:
                if not subscription.plan.is_active:
                    continue
                SubscriptionLifecycleService.renew_subscription(
                    subscription=subscription, renewal_start=current_time
                )
            else:
                SubscriptionLifecycleService.downgrade_to_free(
                    subscription=subscription, downgrade_at=current_time
                )
            processed_count += 1
        return processed_count

    @staticmethod
    @transaction.atomic
    def add_bonus_minutes(*, user, minutes: int) -> UserSubscription:
        """Credit purchased top-up minutes onto the user's current active
        subscription. Callers must have already verified the subscription
        is an active paid plan -- bonus minutes tied to a plan that isn't
        live is meaningless and this does not check that on its own.
        """
        subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .get(user=user, is_active=True)
        )
        subscription.bonus_minutes += minutes
        subscription.save(update_fields=["bonus_minutes", "updated_at"])
        logger.info(
            "bonus_minutes_credited",
            extra={
                "user_id": str(user.id),
                "subscription_id": str(subscription.id),
                "plan_key": subscription.plan.key,
                "minutes": minutes,
                "bonus_minutes": subscription.bonus_minutes,
            },
        )
        return subscription

    @staticmethod
    @transaction.atomic
    def add_referral_minutes(*, user, minutes: int) -> UserSubscription:
        """Credit referral-reward minutes onto the user's live plan allowance,
        creating the Free plan row first when the user has none yet."""
        SubscriptionLifecycleService.get_active_subscription(user=user)
        subscription = (
            UserSubscription.objects.select_for_update()
            .select_related("plan", "user")
            .get(user=user, is_active=True)
        )
        subscription.remaining_minutes += minutes
        subscription.save(update_fields=["remaining_minutes", "updated_at"])
        SubscriptionLifecycleService.sync_legacy_user_fields(user=user, subscription=subscription)
        logger.info(
            "referral_minutes_credited",
            extra={"user_id": str(user.id), "subscription_id": str(subscription.id), "minutes": minutes},
        )
        return subscription

    @staticmethod
    def sync_legacy_user_fields(*, user, subscription: UserSubscription) -> None:
        user.subscription_plan = _LEGACY_PLAN_MAP.get(
            subscription.plan.key, LegacySubscriptionPlan.FREE
        )
        user.remaining_minutes = subscription.remaining_minutes
        user.save(update_fields=["subscription_plan", "remaining_minutes", "updated_at"])
