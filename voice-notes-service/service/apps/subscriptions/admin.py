from django.contrib import admin

from apps.subscriptions.models import SubscriptionPlan, UserSubscription


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ("key", "name", "monthly_minutes", "price_toman", "public_discount_percent", "public_discount_is_active", "is_active")
    list_filter = ("is_active", "public_discount_is_active", "key")
    search_fields = ("key", "name")


@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "plan",
        "remaining_minutes",
        "reserved_minutes",
        "used_minutes",
        "expires_at",
        "granted_by_admin",
        "is_active",
    )
    list_filter = ("is_active", "granted_by_admin", "plan__key")
    search_fields = ("user__phone_number", "user__full_name", "plan__name", "plan__key")

    def save_model(self, request, obj, form, change):
        # Anything staff create or edit here is a manual grant with no payment
        # record; flag it so the hourly sweep doesn't downgrade it to Free.
        if not change or form.has_changed():
            obj.granted_by_admin = True
        super().save_model(request, obj, form, change)
        if obj.is_active:
            from services.subscriptions.lifecycle import SubscriptionLifecycleService

            SubscriptionLifecycleService.sync_legacy_user_fields(user=obj.user, subscription=obj)
