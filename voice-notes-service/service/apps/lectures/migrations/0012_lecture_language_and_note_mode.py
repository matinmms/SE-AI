from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0011_rename_lectures_le_user_id_9d3abf_idx_lectures_le_user_id_c1af02_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecture",
            name="input_language",
            field=models.CharField(
                choices=[
                    ("fa", "fa"),
                    ("en", "en"),
                    ("ar", "ar"),
                    ("tr", "tr"),
                    ("fr", "fr"),
                    ("de", "de"),
                ],
                default="fa",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="lecture",
            name="output_language",
            field=models.CharField(
                choices=[
                    ("fa", "fa"),
                    ("en", "en"),
                    ("ar", "ar"),
                    ("tr", "tr"),
                    ("fr", "fr"),
                    ("de", "de"),
                ],
                default="fa",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="lecture",
            name="note_detail_mode",
            field=models.CharField(
                choices=[("detailed", "Detailed"), ("summarized", "Summarized")],
                default="detailed",
                max_length=16,
            ),
        ),
    ]
