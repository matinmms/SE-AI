from django.db import migrations, models


def enable_initial_paid_plan_campaign(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    SubscriptionPlan.objects.filter(key__in=("PLUS", "PRO")).update(
        public_discount_percent=50,
        public_discount_is_active=True,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0003_rename_subscriptio_is_acti_d40f90_idx_subscriptio_is_acti_2e4cd7_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="subscriptionplan",
            name="public_discount_percent",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="subscriptionplan",
            name="public_discount_is_active",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="subscriptionplan",
            name="public_discount_starts_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="subscriptionplan",
            name="public_discount_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(enable_initial_paid_plan_campaign, migrations.RunPython.noop),
    ]
