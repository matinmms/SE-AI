from django.db import migrations, models
from django.db.models import Sum


def reconcile_reservations(apps, schema_editor):
    """Uploads used to hold minutes immediately; only processing logs should hold them."""
    UsageLog = apps.get_model("usage", "UsageLog")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")

    UsageLog.objects.filter(status="PROCESSING").update(minutes_reserved=True)
    UsageLog.objects.exclude(status="PROCESSING").update(minutes_reserved=False)

    held = {
        row["user_id"]: int(row["total"] or 0)
        for row in UsageLog.objects.filter(minutes_reserved=True)
        .values("user_id")
        .annotate(total=Sum("audio_minutes"))
    }
    for subscription in UserSubscription.objects.filter(is_active=True).iterator():
        target = held.get(subscription.user_id, 0)
        if subscription.reserved_minutes != target:
            subscription.reserved_minutes = target
            subscription.save(update_fields=["reserved_minutes"])


class Migration(migrations.Migration):
    dependencies = [
        ("usage", "0003_usagelog_transcription_metadata"),
        ("subscriptions", "0009_subscription_granted_by_admin"),
    ]

    operations = [
        migrations.AddField(
            model_name="usagelog",
            name="minutes_reserved",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(reconcile_reservations, migrations.RunPython.noop),
    ]
