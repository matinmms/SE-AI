from __future__ import annotations

from django.db import migrations

OLD = {"price_toman": 295000, "original_price_toman": 390000, "discount_percent": 25}
NEW = {"price_toman": 390000, "original_price_toman": 520000, "discount_percent": 25}


def set_plus_price(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    SubscriptionPlan.objects.filter(key="PLUS", **OLD).update(**NEW)


def restore_plus_price(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    SubscriptionPlan.objects.filter(key="PLUS", **NEW).update(**OLD)


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0010_pro_plan_two_months"),
    ]

    operations = [
        migrations.RunPython(set_plus_price, restore_plus_price),
    ]
