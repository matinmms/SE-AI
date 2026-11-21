from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="generatednote",
            name="body_html",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="error_message",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="generation_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="prompt_metadata",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="semantic_payload",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="source_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="status",
            field=models.CharField(
                choices=[
                    ("NOT_STARTED", "Not Started"),
                    ("QUEUED", "Queued"),
                    ("SUMMARIZING", "Summarizing"),
                    ("STRUCTURING", "Structuring"),
                    ("RENDERING", "Rendering"),
                    ("COMPLETED", "Completed"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="NOT_STARTED",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="summary_text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="generatednote",
            name="title",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
