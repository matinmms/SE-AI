import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("lectures", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="LectureFile",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "file_type",
                    models.CharField(
                        choices=[
                            ("audio", "Audio"),
                            ("video", "Video"),
                            ("document", "Document"),
                            ("transcript", "Transcript"),
                        ],
                        db_index=True,
                        max_length=32,
                    ),
                ),
                ("original_name", models.CharField(max_length=255)),
                ("storage_path", models.CharField(max_length=500, unique=True)),
                ("mime_type", models.CharField(max_length=255)),
                ("file_size", models.BigIntegerField()),
                (
                    "lecture",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="files",
                        to="lectures.lecture",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddIndex(
            model_name="lecturefile",
            index=models.Index(fields=["lecture", "file_type"], name="uploads_lec_lecture_3f3ddc_idx"),
        ),
    ]
