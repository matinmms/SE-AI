from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0002_lecture_processing_progress_and_status"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="lecture",
            new_name="lectures_le_course__0d6273_idx",
            old_name="lectures_le_course__710b86_idx",
        ),
    ]
