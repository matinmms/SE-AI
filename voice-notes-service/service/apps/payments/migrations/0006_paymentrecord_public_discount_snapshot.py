from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0005_zibal_payment_gateway"),
        ("subscriptions", "0004_subscriptionplan_public_discount"),
    ]

    operations = [
        migrations.AddField(
            model_name="paymentrecord",
            name="public_discount_percent",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="paymentrecord",
            name="public_discount_amount",
            field=models.PositiveIntegerField(default=0),
        ),
    ]
