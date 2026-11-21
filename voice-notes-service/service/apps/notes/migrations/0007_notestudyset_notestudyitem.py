# Generated manually because local Docker is not running; matches apps.notes.models.

import uuid

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("notes", "0006_generatednote_editor_revision_and_edited_at"),
    ]

    operations = [
        migrations.CreateModel(
            name="NoteStudySet",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("requested_count", models.PositiveSmallIntegerField()),
                ("difficulty", models.CharField(choices=[("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard"), ("mixed", "Mixed")], max_length=16)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("COMPLETED", "Completed"), ("FAILED", "Failed")], db_index=True, default="PENDING", max_length=16)),
                ("generated_count", models.PositiveSmallIntegerField(default=0)),
                ("model_name", models.CharField(blank=True, max_length=120)),
                ("provider_name", models.CharField(blank=True, max_length=64)),
                ("input_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("output_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("total_tokens", models.PositiveIntegerField(blank=True, null=True)),
                ("estimated_cost_usd", models.DecimalField(blank=True, decimal_places=8, max_digits=12, null=True)),
                ("error_message", models.CharField(blank=True, max_length=500)),
                ("note", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="study_sets", to="notes.generatednote")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="note_study_sets", to="accounts.user")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="NoteStudyItem",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("order_index", models.PositiveSmallIntegerField()),
                ("source_section_title", models.CharField(blank=True, max_length=255)),
                ("concept", models.CharField(max_length=255)),
                ("flashcard_front", models.TextField()),
                ("flashcard_back", models.TextField()),
                ("mcq_question", models.TextField()),
                ("options", models.JSONField(default=list)),
                ("correct_option_index", models.PositiveSmallIntegerField()),
                ("explanation", models.TextField(blank=True)),
                ("difficulty", models.CharField(choices=[("easy", "Easy"), ("medium", "Medium"), ("hard", "Hard"), ("mixed", "Mixed")], max_length=16)),
                ("source_quote", models.TextField(blank=True)),
                ("note", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="study_items", to="notes.generatednote")),
                ("study_set", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="items", to="notes.notestudyset")),
                ("user", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="note_study_items", to="accounts.user")),
            ],
            options={"ordering": ("order_index", "created_at")},
        ),
        migrations.AddConstraint(
            model_name="notestudyset",
            constraint=models.UniqueConstraint(condition=Q(status="PENDING"), fields=("note", "requested_count", "difficulty"), name="unique_pending_note_study_set_request"),
        ),
        migrations.AddIndex(
            model_name="notestudyset",
            index=models.Index(fields=["user", "note", "status", "created_at"], name="notes_notes_user_id_92ab3c_idx"),
        ),
        migrations.AddIndex(
            model_name="notestudyset",
            index=models.Index(fields=["note", "requested_count", "difficulty", "status"], name="notes_notes_note_id_3fc276_idx"),
        ),
        migrations.AddConstraint(
            model_name="notestudyitem",
            constraint=models.UniqueConstraint(fields=("study_set", "order_index"), name="unique_note_study_item_position"),
        ),
        migrations.AddConstraint(
            model_name="notestudyitem",
            constraint=models.CheckConstraint(condition=Q(correct_option_index__gte=0, correct_option_index__lte=3), name="note_study_item_correct_option_range"),
        ),
        migrations.AddIndex(
            model_name="notestudyitem",
            index=models.Index(fields=["user", "note", "created_at"], name="notes_notes_user_id_954f36_idx"),
        ),
        migrations.AddIndex(
            model_name="notestudyitem",
            index=models.Index(fields=["study_set", "order_index"], name="notes_notes_study_s8a9a4_idx"),
        ),
    ]
