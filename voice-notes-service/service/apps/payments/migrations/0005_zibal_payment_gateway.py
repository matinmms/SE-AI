from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


def backfill_payment_identity(apps, schema_editor):
    PaymentRecord = apps.get_model("payments", "PaymentRecord")
    for payment in PaymentRecord.objects.all().iterator():
        payment.provider = "manual" if payment.receipt_file else "legacy"
        payment.order_id = str(payment.id)
        payment.gateway_track_id = payment.authority or None
        payment.gateway_amount_rial = int(payment.amount or 0) * 10
        payment.save(
            update_fields=["provider", "order_id", "gateway_track_id", "gateway_amount_rial"]
        )


class Migration(migrations.Migration):
    dependencies = [
        ("payments", "0004_paymentrecord_discount_amount_and_more"),
        ("subscriptions", "0003_rename_subscriptio_is_acti_d40f90_idx_subscriptio_is_acti_2e4cd7_idx_and_more"),
    ]

    operations = [
        migrations.AddField(model_name="paymentrecord", name="provider", field=models.CharField(db_index=True, default="legacy", max_length=32)),
        migrations.AddField(model_name="paymentrecord", name="order_id", field=models.CharField(blank=True, max_length=64, null=True, unique=True)),
        migrations.AddField(model_name="paymentrecord", name="gateway_track_id", field=models.CharField(blank=True, max_length=128, null=True, unique=True)),
        migrations.AddField(model_name="paymentrecord", name="gateway_amount_rial", field=models.PositiveBigIntegerField(default=0)),
        migrations.AddField(model_name="paymentrecord", name="gateway_status", field=models.IntegerField(blank=True, null=True)),
        migrations.AddField(model_name="paymentrecord", name="initiated_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="paymentrecord", name="expires_at", field=models.DateTimeField(blank=True, db_index=True, null=True)),
        migrations.AddField(model_name="paymentrecord", name="verified_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(model_name="paymentrecord", name="fulfilled_at", field=models.DateTimeField(blank=True, null=True)),
        migrations.AddField(
            model_name="paymentrecord",
            name="fulfilled_subscription",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="fulfilling_payments", to="subscriptions.usersubscription"),
        ),
        migrations.AddField(model_name="paymentrecord", name="failure_code", field=models.CharField(blank=True, max_length=64)),
        migrations.AddField(model_name="paymentrecord", name="failure_message", field=models.CharField(blank=True, max_length=500)),
        migrations.AlterField(
            model_name="paymentrecord",
            name="status",
            field=models.CharField(
                choices=[
                    ("CREATED", "Created"), ("REQUESTING", "Requesting"), ("PENDING", "Pending"),
                    ("VERIFYING", "Verifying"), ("SUCCESS", "Success"), ("FAILED", "Failed"),
                    ("CANCELED", "Canceled"), ("EXPIRED", "Expired"),
                ],
                db_index=True,
                default="PENDING",
                max_length=32,
            ),
        ),
        migrations.RunPython(backfill_payment_identity, migrations.RunPython.noop),
        migrations.AddIndex(model_name="paymentrecord", index=models.Index(fields=["provider", "status", "expires_at"], name="payments_pa_provide_525fa8_idx")),
        migrations.AddConstraint(
            model_name="paymentrecord",
            constraint=models.UniqueConstraint(
                condition=models.Q(provider="zibal", status__in=["CREATED", "REQUESTING", "PENDING", "VERIFYING"]),
                fields=("user", "subscription_plan"),
                name="unique_active_zibal_checkout_per_user_plan",
            ),
        ),
        migrations.CreateModel(
            name="DiscountReservation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("status", models.CharField(choices=[("RESERVED", "Reserved"), ("CONSUMED", "Consumed"), ("RELEASED", "Released")], db_index=True, default="RESERVED", max_length=16)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                ("discount_code", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="reservations", to="payments.discountcode")),
                ("payment", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="discount_reservation", to="payments.paymentrecord")),
            ],
        ),
        migrations.AddIndex(model_name="discountreservation", index=models.Index(fields=["discount_code", "status", "expires_at"], name="payments_di_discoun_25e2bd_idx")),
    ]
