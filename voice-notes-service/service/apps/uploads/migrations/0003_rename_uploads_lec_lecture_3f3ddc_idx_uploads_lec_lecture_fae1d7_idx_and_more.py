from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("uploads", "0002_update_upload_fields"),
    ]

    operations = [
        migrations.RenameIndex(
            model_name="lecturefile",
            new_name="uploads_lec_lecture_fae1d7_idx",
            old_name="uploads_lec_lecture_3f3ddc_idx",
        ),
        migrations.RenameIndex(
            model_name="lecturefile",
            new_name="uploads_lec_lecture_a65744_idx",
            old_name="uploads_lec_lecture_f5beea_idx",
        ),
    ]
