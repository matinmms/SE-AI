from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("uploads", "0006_anonymous_audio_upload"),
    ]

    operations = [
        migrations.AddField(
            model_name="uploadattempt",
            name="error_detail",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
