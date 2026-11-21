from django.db import migrations, models
from django.db.models import F


def mark_existing_notes_as_opened(apps, schema_editor):
    GeneratedNote = apps.get_model("notes", "GeneratedNote")
    GeneratedNote.objects.filter(first_opened_at__isnull=True).update(
        first_opened_at=F("created_at")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0011_note_chat_rag_foundation"),
    ]

    operations = [
        migrations.AddField(
            model_name="generatednote",
            name="first_opened_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(mark_existing_notes_as_opened, migrations.RunPython.noop),
    ]
