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
            name="GeneratedNote",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("html_path", models.CharField(blank=True, max_length=500)),
                ("pdf_path", models.CharField(blank=True, max_length=500)),
                (
                    "lecture",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="generated_note",
                        to="lectures.lecture",
                    ),
                ),
            ],
            options={"ordering": ("-created_at",)},
        ),
    ]
