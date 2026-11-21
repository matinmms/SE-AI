from __future__ import annotations

from django.db import migrations


PLAN_DEFAULTS = {
    "FREE": {
        "name": "رایگان",
        "subtitle": "برای شروع و تست TesaNote",
        "monthly_minutes": 60,
        "duration_months": 1,
        "price_toman": 0,
        "original_price_toman": 0,
        "discount_percent": 0,
        "is_featured": False,
        "badge_text": "",
        "benefit_line": "بدون نیاز به پرداخت",
        "display_order": 0,
    },
    "PLUS": {
        "name": "پلاس",
        "subtitle": "پلن سبک",
        "monthly_minutes": 900,
        "duration_months": 1,
        "price_toman": 295000,
        "original_price_toman": 390000,
        "discount_percent": 25,
        "is_featured": False,
        "badge_text": "",
        "benefit_line": "شروع اقتصادی برای استفاده سبک",
        "display_order": 1,
    },
    "PRO": {
        "name": "پرو",
        "subtitle": "پلن دانشجویی",
        "monthly_minutes": 3000,
        "duration_months": 1,
        "price_toman": 795000,
        "original_price_toman": 1060000,
        "discount_percent": 25,
        "is_featured": True,
        "badge_text": "محبوب‌ترین",
        "benefit_line": "هر دقیقه حدود ۱۹٪ به‌صرفه‌تر از پلن پلاس",
        "display_order": 2,
    },
    "MAX": {
        "name": "مکس",
        "subtitle": "پلن ترمی",
        "monthly_minutes": 9000,
        "duration_months": 3,
        "price_toman": 1950000,
        "original_price_toman": 2600000,
        "discount_percent": 25,
        "is_featured": False,
        "badge_text": "۳ ماهه",
        "benefit_line": "۴۳۵ هزار تومان صرفه‌جویی نسبت به خرید ۳ ماه پلن پرو",
        "display_order": 3,
    },
}


def redesign_plan_pricing(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")
    User = apps.get_model("accounts", "User")

    for key, payload in PLAN_DEFAULTS.items():
        plan, _created = SubscriptionPlan.objects.update_or_create(
            key=key,
            defaults={
                **payload,
                "is_active": True,
                # The 2026 launch campaign (50% off PLUS/PRO via the public-discount
                # overlay) is superseded by these plans' own baked-in discounted
                # price_toman -- leaving it active would silently compound with it.
                "public_discount_percent": 0,
                "public_discount_is_active": False,
            },
        )
        active_subscriptions = UserSubscription.objects.filter(plan=plan, is_active=True)
        for subscription in active_subscriptions.iterator():
            # Never take minutes away from someone already on a live cycle;
            # only raise their allowance if the redesigned plan grants more,
            # exactly as the existing free-plan-minutes migration already does.
            updated_remaining = max(
                subscription.remaining_minutes,
                max(0, plan.monthly_minutes - subscription.used_minutes),
            )
            if updated_remaining == subscription.remaining_minutes:
                continue
            subscription.remaining_minutes = updated_remaining
            subscription.save(update_fields=["remaining_minutes", "updated_at"])
            User.objects.filter(id=subscription.user_id).update(remaining_minutes=updated_remaining)


def reverse_noop(apps, schema_editor):
    # Deliberately not reversible: reverting plan pricing/minutes for real
    # subscribers would require re-deriving values this migration cannot
    # safely reconstruct (e.g. which increases were user-specific vs. the
    # blanket raise applied here).
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0006_alter_subscriptionplan_options_and_more"),
    ]

    operations = [
        migrations.RunPython(redesign_plan_pricing, reverse_noop),
    ]
