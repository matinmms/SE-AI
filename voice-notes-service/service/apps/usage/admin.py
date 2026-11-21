from django.contrib import admin

from apps.usage.models import UsageLog


@admin.register(UsageLog)
class UsageLogAdmin(admin.ModelAdmin):
    list_display = ("user", "lecture", "audio_minutes", "total_cost", "status", "started_at", "completed_at")
    list_filter = ("status",)
    search_fields = ("user__phone_number", "user__full_name", "lecture__title")
