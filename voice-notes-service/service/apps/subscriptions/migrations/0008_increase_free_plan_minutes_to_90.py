from __future__ import annotations

from django.db import migrations


FREE_PLAN_MINUTES = 90


def increase_free_plan_minutes(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")
    User = apps.get_model("accounts", "User")

    free_plan = SubscriptionPlan.objects.filter(key="FREE").first()
    if free_plan is None:
        return

    free_plan.monthly_minutes = FREE_PLAN_MINUTES
    free_plan.save(update_fields=["monthly_minutes"])

    active_subscriptions = UserSubscription.objects.filter(plan=free_plan, is_active=True)
    for subscription in active_subscriptions.iterator():
        # Never take minutes away from someone already on a live cycle;
        # only raise their allowance if the new plan grants more, exactly
        # as the earlier 60-minute free-plan migration already did.
        updated_remaining = max(
            subscription.remaining_minutes,
            max(0, FREE_PLAN_MINUTES - subscription.used_minutes),
        )
        if updated_remaining == subscription.remaining_minutes:
            continue
        subscription.remaining_minutes = updated_remaining
        subscription.save(update_fields=["remaining_minutes", "updated_at"])
        User.objects.filter(id=subscription.user_id).update(remaining_minutes=updated_remaining)


def reverse_noop(apps, schema_editor):
    # Deliberately not reversible, matching the earlier free-plan-minutes
    # migrations: reverting would require re-deriving which subscribers'
    # balances were raised by this change versus some other adjustment.
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0007_redesign_plan_pricing_and_seed_max"),
    ]

    operations = [
        migrations.RunPython(increase_free_plan_minutes, reverse_noop),
    ]
