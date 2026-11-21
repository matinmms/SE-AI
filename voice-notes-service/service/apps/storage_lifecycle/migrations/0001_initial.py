# Generated manually from the additive Phase 5 StoredArtifact model.

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("lectures", "0012_lecture_language_and_note_mode"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="StoredArtifact",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("storage_path", models.CharField(max_length=500, unique=True)),
                ("artifact_type", models.CharField(max_length=64)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("storage_class", models.CharField(choices=[("TEMPORARY", "Temporary"), ("PERSISTENT", "Persistent")], db_index=True, max_length=16)),
                ("counts_toward_quota", models.BooleanField(db_index=True, default=False)),
                ("expires_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("cleanup_started_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("cleanup_attempts", models.PositiveSmallIntegerField(default=0)),
                ("cleanup_error", models.CharField(blank=True, max_length=128)),
                ("deleted_at", models.DateTimeField(blank=True, db_index=True, null=True)),
                ("lecture", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="stored_artifacts", to="lectures.lecture")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="stored_artifacts", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["storage_class", "expires_at", "deleted_at"], name="storage_lifecycle_due_idx"),
                    models.Index(fields=["user", "counts_toward_quota", "deleted_at"], name="storage_lifecycle_quota_idx"),
                    models.Index(fields=["lecture", "storage_class"], name="storage_lifecycle_lecture_idx"),
                ],
            },
        ),
    ]
