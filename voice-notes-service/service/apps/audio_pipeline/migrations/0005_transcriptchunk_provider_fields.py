from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audio_pipeline", "0004_rename_audio_pipel_lecture_9a24e1_idx_audio_pipel_lecture_ec8237_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="transcriptchunk",
            name="hotwords_used",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="transcriptchunk",
            name="processing_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="transcriptchunk",
            name="provider_job_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="transcriptchunk",
            name="provider_trace_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="transcriptchunk",
            name="transcript_source",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
