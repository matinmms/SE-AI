from django.contrib import admin

from apps.admin_ops.models import AdminAuditLog, ProcessingError


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "target_user", "entity_type", "entity_id", "created_at")
    list_filter = ("action",)
    search_fields = ("actor__phone_number", "actor__full_name", "target_user__phone_number", "target_user__full_name", "entity_type", "entity_id")


@admin.register(ProcessingError)
class ProcessingErrorAdmin(admin.ModelAdmin):
    list_display = ("stage", "error_type", "user", "lecture", "resolved", "retry_count", "created_at")
    list_filter = ("resolved", "stage", "error_type")
    search_fields = ("user__phone_number", "user__full_name", "lecture__title", "error_message")
