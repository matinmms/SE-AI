from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="lecture",
            name="processing_progress",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="lecture",
            name="status",
            field=models.CharField(
                choices=[
                    ("UPLOADED", "Uploaded"),
                    ("PROCESSING", "Processing"),
                    ("FAILED", "Failed"),
                    ("COMPLETED", "Completed"),
                ],
                db_index=True,
                default="UPLOADED",
                max_length=32,
            ),
        ),
    ]
