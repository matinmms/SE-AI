from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0005_rename_lectures_sl_lecture_9d7e87_idx_lectures_sl_lecture_cbf318_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecture",
            name="audio_processing_progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="lecture",
            name="audio_processing_status",
            field=models.CharField(
                choices=[
                    ("NOT_STARTED", "Not Started"),
                    ("QUEUED", "Queued"),
                    ("NORMALIZING", "Normalizing"),
                    ("CLEANING", "Cleaning"),
                    ("REMOVING_SILENCE", "Removing Silence"),
                    ("SPEED_OPTIMIZING", "Speed Optimizing"),
                    ("CHUNKING", "Chunking"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="NOT_STARTED",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="lecture",
            name="optimized_audio_duration",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lecture",
            name="processing_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="lecture",
            name="silence_removed_seconds",
            field=models.FloatField(default=0),
        ),
    ]
