from django.contrib import admin

from apps.referrals.models import NoteShareLink, ShareEvent, ShareReferral


@admin.register(NoteShareLink)
class NoteShareLinkAdmin(admin.ModelAdmin):
    list_display = ("note", "owner", "created_at", "revoked_at")
    list_filter = ("revoked_at",)
    search_fields = ("owner__full_name", "owner__phone_number", "note__title")
    raw_id_fields = ("note", "owner")
    exclude = ("token",)


@admin.register(ShareEvent)
class ShareEventAdmin(admin.ModelAdmin):
    list_display = ("link", "kind", "user", "created_at")
    list_filter = ("kind",)
    raw_id_fields = ("link", "user")


@admin.register(ShareReferral)
class ShareReferralAdmin(admin.ModelAdmin):
    list_display = ("referrer", "referred_user", "source", "minutes_awarded", "status", "created_at")
    list_filter = ("status", "source")
    raw_id_fields = ("referrer", "referred_user", "link")
