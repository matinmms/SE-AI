from django.contrib import admin

from apps.notes.models import GeneratedNote, NoteFeedback


@admin.register(NoteFeedback)
class NoteFeedbackAdmin(admin.ModelAdmin):
    list_display = ("rating", "user", "note", "allow_public", "created_at")
    list_filter = ("rating", "allow_public", "created_at")
    search_fields = ("user__phone_number", "user__full_name", "user__email", "note__title", "comment")
    raw_id_fields = ("user", "note")
    date_hierarchy = "created_at"


@admin.register(GeneratedNote)
class GeneratedNoteAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "status", "variant", "created_at")
    list_filter = ("status", "variant")
    search_fields = ("title", "user__phone_number", "user__full_name", "lecture__title")
    raw_id_fields = ("user", "lecture", "folder")
    readonly_fields = ("body_html", "semantic_payload", "generation_metadata", "prompt_metadata", "source_snapshot")
