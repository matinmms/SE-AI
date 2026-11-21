from django.db import migrations, models


def create_dispatch_control(apps, schema_editor):
    apps.get_model("ai_pipeline", "FairSchedulerDispatchControl").objects.get_or_create(id=1)


def remove_dispatch_control(apps, schema_editor):
    apps.get_model("ai_pipeline", "FairSchedulerDispatchControl").objects.filter(id=1).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("ai_pipeline", "0002_processing_job_pending_capacity"),
    ]

    operations = [
        migrations.CreateModel(
            name="FairSchedulerDispatchControl",
            fields=[
                (
                    "id",
                    models.PositiveSmallIntegerField(
                        default=1, editable=False, primary_key=True, serialize=False
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Fair scheduler dispatch control",
                "verbose_name_plural": "Fair scheduler dispatch controls",
            },
        ),
        migrations.RunPython(create_dispatch_control, remove_dispatch_control),
        migrations.AddField(
            model_name="lectureprocessingjob",
            name="scheduler_claimed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="lectureprocessingjob",
            name="scheduler_dispatched_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="lectureprocessingjob",
            name="scheduler_dispatch_attempts",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="lectureprocessingjob",
            name="scheduler_last_dispatch_error",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="lectureprocessingjob",
            name="scheduler_managed",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="lectureprocessingjob",
            name="scheduler_ready_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddIndex(
            model_name="lectureprocessingjob",
            index=models.Index(
                fields=["scheduler_managed", "status", "scheduler_ready_at"],
                name="ai_job_scheduler_ready_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="lectureprocessingjob",
            index=models.Index(
                fields=["scheduler_managed", "scheduler_claimed_at"],
                name="ai_job_scheduler_claim_idx",
            ),
        ),
    ]
