from __future__ import annotations

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lectures", "0010_lecture_processing_lifecycle"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminAuditLog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("action", models.CharField(choices=[("QUOTA_ADJUSTMENT", "Quota Adjustment"), ("PAYMENT_EVENT", "Payment Event"), ("SUBSCRIPTION_ACTIVATION", "Subscription Activation"), ("PROCESSING_ERROR_RESOLVED", "Processing Error Resolved"), ("PROCESSING_RETRY_REQUESTED", "Processing Retry Requested")], db_index=True, max_length=64)),
                ("entity_type", models.CharField(db_index=True, max_length=64)),
                ("entity_id", models.CharField(blank=True, max_length=128)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("actor", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="admin_actions_performed", to=settings.AUTH_USER_MODEL)),
                ("target_user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="admin_actions_received", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="ProcessingError",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("stage", models.CharField(db_index=True, max_length=128)),
                ("error_type", models.CharField(db_index=True, max_length=128)),
                ("error_message", models.TextField()),
                ("traceback", models.TextField(blank=True)),
                ("resolved", models.BooleanField(db_index=True, default=False)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("retry_count", models.PositiveIntegerField(default=0)),
                ("metadata", models.JSONField(blank=True, default=dict)),
                ("lecture", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="processing_errors", to="lectures.lecture")),
                ("resolved_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="resolved_processing_errors", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="processing_errors", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("resolved", "-created_at")},
        ),
        migrations.AddIndex(
            model_name="adminauditlog",
            index=models.Index(fields=["action", "created_at"], name="admin_ops_a_action_84fa80_idx"),
        ),
        migrations.AddIndex(
            model_name="adminauditlog",
            index=models.Index(fields=["target_user", "created_at"], name="admin_ops_a_target__12ed99_idx"),
        ),
        migrations.AddIndex(
            model_name="processingerror",
            index=models.Index(fields=["resolved", "created_at"], name="admin_ops_p_resolve_db0303_idx"),
        ),
        migrations.AddIndex(
            model_name="processingerror",
            index=models.Index(fields=["stage", "created_at"], name="admin_ops_p_stage_22480d_idx"),
        ),
        migrations.AddIndex(
            model_name="processingerror",
            index=models.Index(fields=["error_type", "created_at"], name="admin_ops_p_error_t_90f5f3_idx"),
        ),
    ]

