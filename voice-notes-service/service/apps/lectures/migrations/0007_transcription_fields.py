from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0006_audio_processing_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecture",
            name="merged_transcript",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcript_cost",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcript_duration_processed",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcript_language",
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcript_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcription_progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcription_status",
            field=models.CharField(
                choices=[
                    ("NOT_STARTED", "Not Started"),
                    ("QUEUED", "Queued"),
                    ("TRANSCRIBING", "Transcribing"),
                    ("CLEANING", "Cleaning"),
                    ("MERGING", "Merging"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="NOT_STARTED",
                max_length=32,
            ),
        ),
    ]
