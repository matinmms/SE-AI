from __future__ import annotations

from datetime import timedelta

from django.db import migrations
from django.utils import timezone


def seed_and_backfill(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")
    User = apps.get_model("accounts", "User")

    plans = {
        "FREE": {"name": "Free", "monthly_minutes": 30, "price_toman": 0},
        "PLUS": {"name": "Plus", "monthly_minutes": 900, "price_toman": 390000},
        "PRO": {"name": "Pro", "monthly_minutes": 1800, "price_toman": 690000},
    }

    for key, payload in plans.items():
        SubscriptionPlan.objects.update_or_create(key=key, defaults={**payload, "is_active": True})

    free_plan = SubscriptionPlan.objects.get(key="FREE")
    now = timezone.now()
    expires_at = now + timedelta(days=30)
    for user in User.objects.all().iterator():
        if UserSubscription.objects.filter(user=user, is_active=True).exists():
            continue
        remaining_minutes = int(getattr(user, "remaining_minutes", 0) or free_plan.monthly_minutes)
        UserSubscription.objects.create(
            user=user,
            plan=free_plan,
            started_at=now,
            expires_at=expires_at,
            remaining_minutes=remaining_minutes,
            reserved_minutes=0,
            used_minutes=0,
            is_active=True,
        )
        user.subscription_plan = "FREE"
        user.remaining_minutes = remaining_minutes
        user.save(update_fields=["subscription_plan", "remaining_minutes", "updated_at"])


def reverse_seed_and_backfill(apps, schema_editor):
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription.objects.all().delete()
    SubscriptionPlan.objects.filter(key__in=["FREE", "PLUS", "PRO"]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0001_initial"),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_and_backfill, reverse_seed_and_backfill),
    ]

