from django.contrib import admin

from apps.payments.models import DiscountCode, DiscountReservation, PaymentRecord


@admin.register(DiscountCode)
class DiscountCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "title", "percent", "is_active", "redeemed_count", "max_redemptions", "expires_at")
    list_filter = ("is_active",)
    search_fields = ("code", "title")
    filter_horizontal = ("applicable_plans",)


@admin.register(PaymentRecord)
class PaymentRecordAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "subscription_plan",
        "amount",
        "provider",
        "status",
        "gateway_track_id",
        "ref_id",
        "verified_at",
        "fulfilled_at",
    )
    list_filter = ("provider", "status", "subscription_plan__key", "discount_code")
    search_fields = (
        "user__phone_number",
        "user__full_name",
        "order_id",
        "gateway_track_id",
        "ref_id",
        "discount_code_text",
    )
    readonly_fields = (
        "order_id",
        "gateway_track_id",
        "gateway_amount_rial",
        "gateway_status",
        "verified_at",
        "fulfilled_at",
        "fulfilled_subscription",
    )


@admin.register(DiscountReservation)
class DiscountReservationAdmin(admin.ModelAdmin):
    list_display = ("discount_code", "payment", "status", "expires_at", "consumed_at", "released_at")
    list_filter = ("status", "discount_code")
    search_fields = ("discount_code__code", "payment__order_id", "payment__gateway_track_id")
    readonly_fields = ("discount_code", "payment", "expires_at", "consumed_at", "released_at")
