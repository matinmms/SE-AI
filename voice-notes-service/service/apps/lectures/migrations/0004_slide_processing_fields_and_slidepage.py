import django.db.models.deletion
import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0003_rename_lectures_le_course__710b86_idx_lectures_le_course__0d6273_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecture",
            name="slide_processing_debug",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="lecture",
            name="slide_processing_error",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="lecture",
            name="slide_processing_progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="lecture",
            name="slide_processing_stage",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="lecture",
            name="slide_processing_status",
            field=models.CharField(
                choices=[
                    ("NOT_STARTED", "Not Started"),
                    ("QUEUED", "Queued"),
                    ("PROCESSING", "Processing"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="NOT_STARTED",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="lecture",
            name="slide_processing_task_id",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name="lecture",
            name="slides_ready",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="SlidePage",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("page_number", models.PositiveIntegerField()),
                ("text", models.TextField(blank=True)),
                ("structured_text", models.TextField(blank=True)),
                ("images", models.JSONField(blank=True, default=list)),
                ("tables", models.JSONField(blank=True, default=list)),
                ("formulas", models.JSONField(blank=True, default=list)),
                ("ocr_text", models.TextField(blank=True)),
                ("raw_extracted_text", models.TextField(blank=True)),
                ("thumbnail_path", models.CharField(blank=True, max_length=500)),
                (
                    "lecture",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="slide_pages",
                        to="lectures.lecture",
                    ),
                ),
            ],
            options={
                "ordering": ("page_number",),
            },
        ),
        migrations.AddConstraint(
            model_name="slidepage",
            constraint=models.UniqueConstraint(fields=("lecture", "page_number"), name="unique_slide_page_per_lecture"),
        ),
        migrations.AddIndex(
            model_name="slidepage",
            index=models.Index(fields=["lecture", "page_number"], name="lectures_sl_lecture_9d7e87_idx"),
        ),
    ]
