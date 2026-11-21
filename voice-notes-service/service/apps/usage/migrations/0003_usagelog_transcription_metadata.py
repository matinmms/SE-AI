from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("usage", "0002_rename_usage_usage_user_id_d8ff8a_idx_usage_usage_user_id_3c098f_idx_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="usagelog",
            name="transcription_audio_seconds",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="usagelog",
            name="transcription_audio_size_mb",
            field=models.FloatField(default=0),
        ),
        migrations.AddField(
            model_name="usagelog",
            name="transcription_chunks_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="usagelog",
            name="transcription_error_message",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="usagelog",
            name="transcription_fallback_used",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="usagelog",
            name="transcription_latency_ms",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="usagelog",
            name="transcription_model_used",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="usagelog",
            name="transcription_provider_used",
            field=models.CharField(blank=True, max_length=128),
        ),
    ]
