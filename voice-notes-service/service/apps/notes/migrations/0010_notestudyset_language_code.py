from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0009_generatednotesummary"),
    ]

    operations = [
        migrations.AddField(
            model_name="notestudyset",
            name="language_code",
            field=models.CharField(db_index=True, default="fa", max_length=8),
        ),
        migrations.RemoveIndex(
            model_name="notestudyset",
            name="notes_notes_note_id_3fc276_idx",
        ),
        migrations.AddIndex(
            model_name="notestudyset",
            index=models.Index(
                fields=["note", "language_code", "requested_count", "difficulty", "status"],
                name="note_set_language_lookup_idx",
            ),
        ),
    ]
