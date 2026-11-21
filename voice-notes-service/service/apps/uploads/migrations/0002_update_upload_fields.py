from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0002_lecture_processing_progress_and_status"),
        ("uploads", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecturefile",
            name="duration_seconds",
            field=models.PositiveIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="lecturefile",
            name="metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="lecturefile",
            name="upload_status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("UPLOADING", "Uploading"),
                    ("UPLOADED", "Uploaded"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="PENDING",
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="lecturefile",
            name="file_type",
            field=models.CharField(
                choices=[("AUDIO", "Audio"), ("SLIDES", "Slides")],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.AddIndex(
            model_name="lecturefile",
            index=models.Index(fields=["lecture", "upload_status"], name="uploads_lec_lecture_f5beea_idx"),
        ),
    ]
