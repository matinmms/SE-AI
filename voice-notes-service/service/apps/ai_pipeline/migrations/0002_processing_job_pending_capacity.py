from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("ai_pipeline", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="lectureprocessingjob",
            name="status",
            field=models.CharField(
                choices=[
                    ("PENDING", "Pending"),
                    ("QUEUED", "Queued"),
                    ("PROCESSING", "Processing"),
                    ("SUCCEEDED", "Succeeded"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="QUEUED",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="lectureprocessingjob",
            constraint=models.UniqueConstraint(
                condition=Q(("status__in", ["QUEUED", "PROCESSING"])),
                fields=("user",),
                name="one_active_processing_job_per_user",
            ),
        ),
        migrations.AddConstraint(
            model_name="lectureprocessingjob",
            constraint=models.UniqueConstraint(
                condition=Q(("status", "PENDING")),
                fields=("user",),
                name="one_pending_processing_job_per_user",
            ),
        ),
    ]
