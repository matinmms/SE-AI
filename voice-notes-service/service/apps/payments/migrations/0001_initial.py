from __future__ import annotations

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("subscriptions", "0002_seed_plans_and_backfill"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentRecord",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("amount", models.PositiveIntegerField()),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("SUCCESS", "Success"), ("FAILED", "Failed"), ("CANCELED", "Canceled")], db_index=True, default="PENDING", max_length=32)),
                ("authority", models.CharField(blank=True, max_length=128, null=True, unique=True)),
                ("ref_id", models.CharField(blank=True, max_length=128, null=True, unique=True)),
                ("gateway_response", models.JSONField(blank=True, default=dict)),
                ("payment_url", models.URLField(blank=True, max_length=1000)),
                ("paid_at", models.DateTimeField(blank=True, null=True)),
                ("subscription_plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payments", to="subscriptions.subscriptionplan")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="payments", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddIndex(
            model_name="paymentrecord",
            index=models.Index(fields=["user", "status"], name="payments_pa_user_id_4581e3_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentrecord",
            index=models.Index(fields=["subscription_plan", "status"], name="payments_pa_subscri_9122bc_idx"),
        ),
        migrations.AddIndex(
            model_name="paymentrecord",
            index=models.Index(fields=["created_at"], name="payments_pa_created_eb1cc7_idx"),
        ),
    ]

