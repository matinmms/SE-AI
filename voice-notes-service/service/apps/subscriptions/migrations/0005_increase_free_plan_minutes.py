from django.db import migrations


FREE_PLAN_MINUTES = 60


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
        # Reserved minutes remain part of the existing allowance. Only completed
        # usage reduces the new sixty-minute monthly allocation.
        updated_remaining = max(
            subscription.remaining_minutes,
            max(0, FREE_PLAN_MINUTES - subscription.used_minutes),
        )
        if updated_remaining == subscription.remaining_minutes:
            continue
        subscription.remaining_minutes = updated_remaining
        subscription.save(update_fields=["remaining_minutes", "updated_at"])
        User.objects.filter(id=subscription.user_id).update(remaining_minutes=updated_remaining)


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0004_subscriptionplan_public_discount"),
    ]

    operations = [
        migrations.RunPython(increase_free_plan_minutes, migrations.RunPython.noop),
    ]
