from django.contrib import admin

from apps.wallet.models import TopupOffer, WalletAccount, WalletGiftLot, WalletHold, WalletTransaction


@admin.register(WalletAccount)
class WalletAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "paid_toman", "gift_toman", "held_toman", "updated_at")
    search_fields = ("user__phone_number",)
    readonly_fields = ("user", "paid_toman", "gift_toman", "held_toman")


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "user", "kind", "amount_toman", "balance_after", "ref_type")
    list_filter = ("kind",)
    search_fields = ("user__phone_number", "ref_id", "idempotency_key")
    readonly_fields = [field.name for field in WalletTransaction._meta.fields]


admin.site.register(WalletGiftLot)
admin.site.register(WalletHold)
admin.site.register(TopupOffer)
