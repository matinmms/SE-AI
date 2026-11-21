from __future__ import annotations

from dateutil.relativedelta import relativedelta
from django.db import migrations
from django.utils import timezone

OLD_MAX_BENEFIT_LINE = "۴۳۵ هزار تومان صرفه‌جویی نسبت به خرید ۳ ماه پلن پرو"
NEW_MAX_BENEFIT_LINE = "هر دقیقه حدود ۱۸٪ به‌صرفه‌تر از پلن پرو"


def make_pro_two_months(apps, schema_editor):
    SubscriptionPlan = apps.get_model("subscriptions", "SubscriptionPlan")
    UserSubscription = apps.get_model("subscriptions", "UserSubscription")

    pro = SubscriptionPlan.objects.filter(key="PRO").first()
    if pro is None or pro.duration_months >= 2:
        return
    pro.duration_months = 2
    pro.save(update_fields=["duration_months", "updated_at"])

    SubscriptionPlan.objects.filter(key="MAX", benefit_line=OLD_MAX_BENEFIT_LINE).update(
        benefit_line=NEW_MAX_BENEFIT_LINE
    )

    # Live Pro subscriptions get the extra month; already-expired ones are not
    # revived (the reset sweep downgrades them as before).
    now = timezone.now()
    for subscription in UserSubscription.objects.filter(
        plan=pro, is_active=True, expires_at__gt=now
    ).iterator():
        subscription.expires_at = subscription.expires_at + relativedelta(months=1)
        subscription.save(update_fields=["expires_at", "updated_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("subscriptions", "0009_subscription_granted_by_admin"),
    ]

    operations = [
        migrations.RunPython(make_pro_two_months, migrations.RunPython.noop),
    ]
