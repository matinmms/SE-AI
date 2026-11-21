from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("audio_pipeline", "0001_initial"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="audiochunk",
            new_name="audio_pipel_lecture_cf71b6_idx",
            old_name="audio_pipe_lecture_89e345_idx",
        ),
    ]
