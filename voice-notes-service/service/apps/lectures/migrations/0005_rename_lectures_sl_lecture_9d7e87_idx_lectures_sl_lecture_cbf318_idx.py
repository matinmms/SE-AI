from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("lectures", "0004_slide_processing_fields_and_slidepage"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="slidepage",
            new_name="lectures_sl_lecture_cbf318_idx",
            old_name="lectures_sl_lecture_9d7e87_idx",
        ),
    ]
