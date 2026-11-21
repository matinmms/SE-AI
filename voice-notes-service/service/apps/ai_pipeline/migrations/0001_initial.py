from __future__ import annotations

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("lectures", "0012_lecture_language_and_note_mode"),
        ("usage", "0003_usagelog_transcription_metadata"),
    ]

    operations = [
        migrations.CreateModel(
            name="LectureProcessingJob",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("attempt", models.PositiveIntegerField(default=1)),
                ("idempotency_key", models.CharField(max_length=64, unique=True)),
                ("status", models.CharField(choices=[("QUEUED", "Queued"), ("PROCESSING", "Processing"), ("SUCCEEDED", "Succeeded"), ("FAILED", "Failed")], db_index=True, default="QUEUED", max_length=16)),
                ("stage", models.CharField(default="QUEUED", max_length=64)),
                ("progress", models.PositiveSmallIntegerField(default=0)),
                ("celery_task_id", models.CharField(blank=True, db_index=True, max_length=255)),
                ("queued_at", models.DateTimeField()),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("failure_category", models.CharField(blank=True, choices=[("DISPATCH_FAILED", "Dispatch failed"), ("TRANSCRIPTION_FAILED", "Transcription failed"), ("GENERATION_FAILED", "Generation failed"), ("PDF_FAILED", "PDF failed"), ("INTERNAL_ERROR", "Internal error")], max_length=32)),
                ("lecture", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="processing_jobs", to="lectures.lecture")),
                ("usage_log", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="processing_jobs", to="usage.usagelog")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lecture_processing_jobs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(
            model_name="lectureprocessingjob",
            constraint=models.UniqueConstraint(fields=("lecture", "attempt"), name="unique_processing_job_attempt"),
        ),
        migrations.AddIndex(
            model_name="lectureprocessingjob",
            index=models.Index(fields=["lecture", "status"], name="ai_job_lecture_status_idx"),
        ),
        migrations.AddIndex(
            model_name="lectureprocessingjob",
            index=models.Index(fields=["user", "status", "created_at"], name="ai_job_user_status_idx"),
        ),
    ]
