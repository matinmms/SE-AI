import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("lectures", "0005_rename_lectures_sl_lecture_9d7e87_idx_lectures_sl_lecture_cbf318_idx"),
    ]

    operations = [
        migrations.CreateModel(
            name="AudioChunk",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("chunk_index", models.PositiveIntegerField()),
                ("start_ts", models.FloatField()),
                ("end_ts", models.FloatField()),
                ("duration_seconds", models.FloatField()),
                ("original_audio_path", models.CharField(max_length=500)),
                ("optimized_audio_path", models.CharField(max_length=500)),
                (
                    "lecture",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="audio_chunks",
                        to="lectures.lecture",
                    ),
                ),
            ],
            options={
                "ordering": ("chunk_index",),
            },
        ),
        migrations.AddConstraint(
            model_name="audiochunk",
            constraint=models.UniqueConstraint(fields=("lecture", "chunk_index"), name="unique_audio_chunk_per_lecture"),
        ),
        migrations.AddIndex(
            model_name="audiochunk",
            index=models.Index(fields=["lecture", "chunk_index"], name="audio_pipe_lecture_89e345_idx"),
        ),
    ]
