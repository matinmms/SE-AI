from __future__ import annotations

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lectures", "0009_lecture_duration_seconds_lecture_user_and_more"),
        ("subscriptions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="UsageLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("audio_minutes", models.PositiveIntegerField(default=0)),
                ("avenger_cost", models.PositiveIntegerField(default=0)),
                ("llm_cost", models.PositiveIntegerField(default=0)),
                ("pdf_cost", models.PositiveIntegerField(default=0)),
                ("storage_cost", models.PositiveIntegerField(default=0)),
                ("total_cost", models.PositiveIntegerField(default=0)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("PROCESSING", "Processing"), ("COMPLETED", "Completed"), ("FAILED", "Failed")], db_index=True, default="PENDING", max_length=32)),
                ("error_message", models.TextField(blank=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("lecture", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="usage_logs", to="lectures.lecture")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="usage_logs", to=settings.AUTH_USER_MODEL)),
                ("user_subscription", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="usage_logs", to="subscriptions.usersubscription")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddIndex(
            model_name="usagelog",
            index=models.Index(fields=["user", "created_at"], name="usage_usage_user_id_d8ff8a_idx"),
        ),
        migrations.AddIndex(
            model_name="usagelog",
            index=models.Index(fields=["lecture", "created_at"], name="usage_usage_lecture_9e1511_idx"),
        ),
        migrations.AddIndex(
            model_name="usagelog",
            index=models.Index(fields=["status", "created_at"], name="usage_usage_status_68080c_idx"),
        ),
    ]
