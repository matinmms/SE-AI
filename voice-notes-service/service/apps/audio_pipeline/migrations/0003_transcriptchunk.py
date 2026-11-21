import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audio_pipeline", "0002_rename_audio_pipe_lecture_89e345_idx_audio_pipel_lecture_cf71b6_idx"),
        ("lectures", "0007_transcription_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="TranscriptChunk",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("chunk_index", models.PositiveIntegerField()),
                ("text", models.TextField(blank=True)),
                ("raw_text", models.TextField(blank=True)),
                ("start_ts", models.FloatField()),
                ("end_ts", models.FloatField()),
                ("language", models.CharField(blank=True, max_length=32)),
                ("api_cost", models.FloatField(default=0)),
                ("processing_time_ms", models.PositiveIntegerField(default=0)),
                ("confidence_score", models.FloatField(blank=True, null=True)),
                ("raw_response", models.JSONField(blank=True, default=dict)),
                (
                    "audio_chunk",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transcript_chunk",
                        to="audio_pipeline.audiochunk",
                    ),
                ),
                (
                    "lecture",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transcript_chunks",
                        to="lectures.lecture",
                    ),
                ),
            ],
            options={
                "ordering": ("chunk_index",),
            },
        ),
        migrations.AddConstraint(
            model_name="transcriptchunk",
            constraint=models.UniqueConstraint(fields=("lecture", "chunk_index"), name="unique_transcript_chunk_per_lecture"),
        ),
        migrations.AddIndex(
            model_name="transcriptchunk",
            index=models.Index(fields=["lecture", "chunk_index"], name="audio_pipel_lecture_9a24e1_idx"),
        ),
    ]
