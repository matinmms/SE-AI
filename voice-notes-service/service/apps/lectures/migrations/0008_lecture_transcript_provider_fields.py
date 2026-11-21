from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0007_transcription_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecture",
            name="hotword_source",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="lecture",
            name="hotwords_generated",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcript_cost_estimation",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcript_debug_data",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="lecture",
            name="transcript_provider",
            field=models.CharField(blank=True, max_length=64),
        ),
    ]
