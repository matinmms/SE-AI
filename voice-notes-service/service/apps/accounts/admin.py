from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from apps.accounts.models import User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("-created_at",)
    list_display = ("phone_number", "full_name", "email", "subscription_plan", "remaining_minutes", "accuracy_check_enabled", "is_active", "is_staff")
    list_filter = ("accuracy_check_enabled", "is_active", "is_staff")
    search_fields = ("phone_number", "full_name", "email")
    readonly_fields = ("last_login", "created_at", "updated_at")
    fieldsets = (
        (None, {"fields": ("phone_number", "email", "password")}),
        ("Profile", {"fields": ("full_name", "subscription_plan", "remaining_minutes", "accuracy_check_enabled")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "created_at", "updated_at")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("phone_number", "email", "full_name", "password1", "password2", "is_active", "is_staff"),
            },
        ),
    )
