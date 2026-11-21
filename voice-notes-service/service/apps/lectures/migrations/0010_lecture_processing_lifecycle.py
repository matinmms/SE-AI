from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0009_lecture_duration_seconds_lecture_user_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecture",
            name="processing_completed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lecture",
            name="processing_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="lecture",
            name="processing_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lecture",
            name="processing_state",
            field=models.CharField(
                choices=[
                    ("UPLOADED", "Uploaded"),
                    ("QUEUED", "Queued"),
                    ("TRANSCRIBING", "Transcribing"),
                    ("ANALYZING_SLIDES", "Analyzing Slides"),
                    ("GENERATING_NOTES", "Generating Notes"),
                    ("GENERATING_PDF", "Generating PDF"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="UPLOADED",
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="lecture",
            index=models.Index(fields=["user", "processing_state"], name="lectures_le_user_id_9d3abf_idx"),
        ),
    ]

