from __future__ import annotations

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SubscriptionPlan",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("key", models.CharField(choices=[("FREE", "Free"), ("PLUS", "Plus"), ("PRO", "Pro")], db_index=True, max_length=32, unique=True)),
                ("name", models.CharField(max_length=64)),
                ("monthly_minutes", models.PositiveIntegerField()),
                ("price_toman", models.PositiveIntegerField()),
                ("is_active", models.BooleanField(db_index=True, default=True)),
            ],
            options={"ordering": ("price_toman", "created_at")},
        ),
        migrations.CreateModel(
            name="UserSubscription",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(db_index=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("remaining_minutes", models.PositiveIntegerField()),
                ("reserved_minutes", models.PositiveIntegerField(default=0)),
                ("used_minutes", models.PositiveIntegerField(default=0)),
                ("is_active", models.BooleanField(db_index=True, default=True)),
                ("plan", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="subscriptions", to="subscriptions.subscriptionplan")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="subscriptions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddIndex(
            model_name="subscriptionplan",
            index=models.Index(fields=["is_active", "key"], name="subscriptio_is_acti_d40f90_idx"),
        ),
        migrations.AddIndex(
            model_name="usersubscription",
            index=models.Index(fields=["user", "is_active"], name="subscriptio_user_id_25a5d5_idx"),
        ),
        migrations.AddIndex(
            model_name="usersubscription",
            index=models.Index(fields=["expires_at", "is_active"], name="subscriptio_expires_02490c_idx"),
        ),
        migrations.AddConstraint(
            model_name="usersubscription",
            constraint=models.UniqueConstraint(condition=models.Q(("is_active", True)), fields=("user",), name="unique_active_subscription_per_user"),
        ),
    ]
