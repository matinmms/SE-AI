# Generated manually because local Docker is not running; matches apps.notes.models.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0007_notestudyset_notestudyitem"),
    ]

    operations = [
        migrations.AddField(
            model_name="notestudyitem",
            name="flashcard_status",
            field=models.CharField(
                choices=[("new", "New"), ("known", "Known"), ("review", "Review")],
                db_index=True,
                default="new",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="notestudyitem",
            name="flashcard_reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="notestudyitem",
            index=models.Index(fields=["user", "note", "flashcard_status"], name="note_item_user_status_idx"),
        ),
    ]
