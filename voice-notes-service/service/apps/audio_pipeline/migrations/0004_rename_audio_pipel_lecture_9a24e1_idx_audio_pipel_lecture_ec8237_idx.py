from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("audio_pipeline", "0003_transcriptchunk"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="transcriptchunk",
            new_name="audio_pipel_lecture_ec8237_idx",
            old_name="audio_pipel_lecture_9a24e1_idx",
        ),
    ]
