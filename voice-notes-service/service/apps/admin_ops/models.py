from __future__ import annotations

from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models

from apps.common.models import BaseModel


class AdminAuditAction(models.TextChoices):
    QUOTA_ADJUSTMENT = "QUOTA_ADJUSTMENT", "Quota Adjustment"
    PAYMENT_EVENT = "PAYMENT_EVENT", "Payment Event"
    SUBSCRIPTION_ACTIVATION = "SUBSCRIPTION_ACTIVATION", "Subscription Activation"
    PROCESSING_ERROR_RESOLVED = "PROCESSING_ERROR_RESOLVED", "Processing Error Resolved"
    PROCESSING_RETRY_REQUESTED = "PROCESSING_RETRY_REQUESTED", "Processing Retry Requested"
    PROCESSING_CANCELLED = "PROCESSING_CANCELLED", "Processing Cancelled"
    PLAN_PRICE_UPDATED = "PLAN_PRICE_UPDATED", "Plan Price Updated"
    REVENUE_TIMELINE_UPDATED = "REVENUE_TIMELINE_UPDATED", "Revenue Timeline Updated"
    EMERGENCY_MODE_CHANGED = "EMERGENCY_MODE_CHANGED", "Emergency Mode Changed"


class ProductEventName(models.TextChoices):
    PAGE_VIEW = "PAGE_VIEW", "Page View"
    PAGE_EXIT = "PAGE_EXIT", "Page Exit"
    CTA_START_FREE_CLICKED = "CTA_START_FREE_CLICKED", "Start Free CTA Clicked"
    UPLOAD_INTENT_CLICKED = "UPLOAD_INTENT_CLICKED", "Upload Intent Clicked"
    PRICING_VIEWED = "PRICING_VIEWED", "Pricing Viewed"
    AUTH_STARTED = "AUTH_STARTED", "Authentication Started"
    OTP_SENT = "OTP_SENT", "OTP Sent"
    AUTHENTICATED = "AUTHENTICATED", "Authenticated"
    UPLOAD_OPENED = "UPLOAD_OPENED", "Upload Opened"
    GUEST_FILE_SELECTED = "GUEST_FILE_SELECTED", "Guest File Selected"
    GUEST_PROCESS_CLICKED = "GUEST_PROCESS_CLICKED", "Guest Process Clicked"
    PAYMENT_CHECKOUT_VIEWED = "PAYMENT_CHECKOUT_VIEWED", "Payment Checkout Viewed"
    PAYMENT_STARTED = "PAYMENT_STARTED", "Payment Started"
    PAYMENT_COMPLETED = "PAYMENT_COMPLETED", "Payment Completed"
    # Landing / upload-first funnel.
    LANDING_VIEW = "LANDING_VIEW", "Landing View"
    CTA_UPLOAD_CLICK = "CTA_UPLOAD_CLICK", "Upload CTA Click"
    DEMO_SCROLLED_50 = "DEMO_SCROLLED_50", "Demo Scrolled 50%"
    DEMO_COMPLETED = "DEMO_COMPLETED", "Demo Completed"
    FILE_SELECTED = "FILE_SELECTED", "File Selected"
    # A chosen file the page refused before upload; metadata.source says why.
    FILE_REJECTED = "FILE_REJECTED", "File Rejected"
    ANON_UPLOAD_COMPLETED = "ANON_UPLOAD_COMPLETED", "Anonymous Upload Completed"
    BUILD_CLICK = "BUILD_CLICK", "Build Note Click"
    PROCESSING_STARTED = "PROCESSING_STARTED", "Processing Started"
    FIRST_NOTE_READY = "FIRST_NOTE_READY", "First Note Ready"
    FIRST_NOTE_OPENED = "FIRST_NOTE_OPENED", "First Note Opened"
    PAYWALL_SHOWN = "PAYWALL_SHOWN", "Paywall Shown"
    PLAN_SELECTED = "PLAN_SELECTED", "Plan Selected"
    CHECKOUT_STARTED = "CHECKOUT_STARTED", "Checkout Started"
    DISCOUNT_EXPIRED = "DISCOUNT_EXPIRED", "First-purchase Discount Expired"
    # Value events: recorded server-side (or via an owner-checked note endpoint),
    # at most once per user/note/Tehran day, with only {"note_id"} as metadata.
    SUMMARY_USED = "SUMMARY_USED", "Summary Used"
    FLASHCARDS_USED = "FLASHCARDS_USED", "Flashcards Used"
    QUIZ_USED = "QUIZ_USED", "Quiz Used"
    # Accuracy check (note review) interactions; metadata carries note_id and optionally spot_id.
    REVIEW_CHIP_SHOWN = "REVIEW_CHIP_SHOWN", "Review Chip Shown"
    REVIEW_CHIP_TAPPED = "REVIEW_CHIP_TAPPED", "Review Chip Tapped"
    REVIEW_SPOT_OPENED = "REVIEW_SPOT_OPENED", "Review Spot Opened"
    REVIEW_CLIP_PLAYED = "REVIEW_CLIP_PLAYED", "Review Clip Played"
    REVIEW_CLIP_ENDED = "REVIEW_CLIP_ENDED", "Review Clip Played To End"
    REVIEW_EDIT_FROM_SPOT = "REVIEW_EDIT_FROM_SPOT", "Review Edit From Spot"
    REVIEW_CHIP_HIDDEN = "REVIEW_CHIP_HIDDEN", "Review Chip Hidden"
    # New-user journey tracking: where a visit came from, and what was done in the charge popup.
    SESSION_ENTRY = "SESSION_ENTRY", "Session Entry"
    WALLET_TOPUP_STARTED = "WALLET_TOPUP_STARTED", "Wallet Top-up Started"
    WALLET_POPUP_CLOSED = "WALLET_POPUP_CLOSED", "Wallet Popup Closed"
    WALLET_TIER_SELECTED = "WALLET_TIER_SELECTED", "Wallet Tier Selected"
    WALLET_AMOUNT_TYPED = "WALLET_AMOUNT_TYPED", "Wallet Amount Typed"
    WALLET_GIFT_SKIP_PROMPT = "WALLET_GIFT_SKIP_PROMPT", "Wallet Gift Skip Prompt"
    WALLET_GIFT_KEPT = "WALLET_GIFT_KEPT", "Wallet Gift Kept"
    WALLET_GIFT_SKIPPED = "WALLET_GIFT_SKIPPED", "Wallet Gift Skipped"
    # Note style choice: the sheet at start, the style picked, saving it, changing it, extra versions.
    NOTE_STYLE_SHEET_SHOWN = "NOTE_STYLE_SHEET_SHOWN", "Note Style Sheet Shown"
    NOTE_STYLE_SELECTED = "NOTE_STYLE_SELECTED", "Note Style Selected"
    NOTE_STYLE_CHANGE_OPENED = "NOTE_STYLE_CHANGE_OPENED", "Note Style Change Opened"
    NOTE_STYLE_SAVED = "NOTE_STYLE_SAVED", "Note Style Saved"
    NOTE_VERSION_MENU_OPENED = "NOTE_VERSION_MENU_OPENED", "Note Version Menu Opened"
    NOTE_VERSION_REQUESTED = "NOTE_VERSION_REQUESTED", "Note Version Requested"
    NOTE_VERSION_OPENED = "NOTE_VERSION_OPENED", "Note Version Opened"


VALUE_EVENT_NAMES = frozenset(
    {
        ProductEventName.SUMMARY_USED,
        ProductEventName.FLASHCARDS_USED,
        ProductEventName.QUIZ_USED,
    }
)


class RevenueTimelineSettings(BaseModel):
    key = models.CharField(max_length=32, unique=True, default="primary")
    lifetime_start_date = models.DateField(null=True, blank=True)

    class Meta:
        verbose_name = "Revenue timeline settings"
        verbose_name_plural = "Revenue timeline settings"

    def __str__(self) -> str:
        return "Revenue timeline settings"


class SystemMode(BaseModel):
    """The single switch for emergency mode (international internet down). Off unless an admin turns it on."""

    key = models.CharField(max_length=32, unique=True, default="primary")
    emergency_active = models.BooleanField(default=False)
    activated_at = models.DateTimeField(null=True, blank=True)
    activated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "System mode"
        verbose_name_plural = "System mode"

    def __str__(self) -> str:
        return f"emergency={'on' if self.emergency_active else 'off'}"


class EmergencyPeriod(BaseModel):
    """One stretch of emergency mode; its length is added back to paid plans when it ends."""

    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reason = models.CharField(max_length=255, blank=True)
    # Set once the paid plans have been extended, so a retry never extends them twice.
    plans_extended_at = models.DateTimeField(null=True, blank=True)
    plans_extended_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-started_at",)


class AdminAuditLog(BaseModel):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_actions_performed",
    )
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="admin_actions_received",
    )
    action = models.CharField(max_length=64, choices=AdminAuditAction.choices, db_index=True)
    entity_type = models.CharField(max_length=64, db_index=True)
    entity_id = models.CharField(max_length=128, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["action", "created_at"]),
            models.Index(fields=["target_user", "created_at"]),
        ]


class ProductEvent(BaseModel):
    """Minimal first-party product analytics; never stores note/chat content or IP addresses."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="product_events",
    )
    name = models.CharField(max_length=48, choices=ProductEventName.choices, db_index=True)
    visitor_id = models.CharField(max_length=64, blank=True, db_index=True)
    session_id = models.CharField(max_length=64, blank=True, db_index=True)
    path = models.CharField(max_length=240, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["name", "created_at"], name="admin_ops_p_name_7d1b8b_idx"),
            models.Index(fields=["visitor_id", "created_at"], name="admin_ops_p_visitor_722a93_idx"),
            models.Index(fields=["user", "created_at"], name="admin_ops_p_user_id_d36f11_idx"),
        ]


class GrowthCampaign(BaseModel):
    code = models.CharField(
        max_length=32,
        unique=True,
        validators=[RegexValidator(r"^[a-z0-9_-]+$", "Use lowercase letters, numbers, _ or -.")],
    )
    name = models.CharField(max_length=120)
    source = models.CharField(max_length=64)
    message_version = models.CharField(max_length=32, blank=True)
    source_group_name = models.CharField(max_length=120, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return self.code


class GrowthVisit(BaseModel):
    """One first-touch campaign per anonymous browser visitor."""

    visitor_id = models.UUIDField(unique=True)
    campaign = models.ForeignKey(GrowthCampaign, on_delete=models.PROTECT, related_name="visits")
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="growth_signup_visit",
    )
    signed_up_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["campaign", "created_at"])]


class GrowthRecipientLink(BaseModel):
    """Opaque per-recipient attribution; intentionally contains no channel identity."""

    token = models.CharField(max_length=64, unique=True)
    campaign = models.ForeignKey(
        GrowthCampaign, on_delete=models.CASCADE, related_name="recipient_links"
    )
    visit = models.OneToOneField(
        GrowthVisit,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="recipient_link",
    )
    first_clicked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["campaign", "first_clicked_at"])]


class ProcessingError(BaseModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_errors",
    )
    lecture = models.ForeignKey(
        "lectures.Lecture",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processing_errors",
    )
    stage = models.CharField(max_length=128, db_index=True)
    error_type = models.CharField(max_length=128, db_index=True)
    error_message = models.TextField()
    traceback = models.TextField(blank=True)
    resolved = models.BooleanField(default=False, db_index=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="resolved_processing_errors",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    retry_count = models.PositiveIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("resolved", "-created_at")
        indexes = [
            models.Index(fields=["resolved", "created_at"]),
            models.Index(fields=["stage", "created_at"]),
            models.Index(fields=["error_type", "created_at"]),
        ]


class PerfMetricBucket(BaseModel):
    """Hourly latency aggregate (count/sum/max + fixed histogram), never raw samples.

    Written in batches by `services.analytics.perf`, so measuring latency
    costs one in-memory increment per request and a handful of upserts a minute.
    """

    kind = models.CharField(max_length=16, db_index=True)
    metric = models.CharField(max_length=24)
    label = models.CharField(max_length=120)
    hour = models.DateTimeField(db_index=True)
    count = models.PositiveIntegerField(default=0)
    sum_ms = models.BigIntegerField(default=0)
    max_ms = models.PositiveIntegerField(default=0)
    buckets = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "metric", "label", "hour"], name="unique_perf_metric_bucket")
        ]


class NoteEngagement(BaseModel):
    """How one person used one of their notes during one visit to the note page.

    The browser reports running totals every few seconds, so a row only ever grows:
    the server keeps the maximum of every counter and a retried report changes nothing.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="note_engagements")
    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="engagements")
    session_id = models.CharField(max_length=64)
    # Seconds the page was visible and the person was doing something (not an idle open tab).
    active_seconds = models.PositiveIntegerField(default=0)
    # Furthest point of the note scrolled into view, 0-100.
    max_scroll_percent = models.PositiveSmallIntegerField(default=0)
    # Edits made in the editor (typing, highlights, inserted blocks...).
    edit_events = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["note", "user", "session_id"], name="unique_note_engagement_visit"),
        ]
        indexes = [models.Index(fields=["note", "-created_at"]), models.Index(fields=["user", "-created_at"])]



class CallReason(models.TextChoices):
    PRICE = "PRICE", "Price / cannot pay"
    NO_NEED = "NO_NEED", "No need right now"
    QUALITY = "QUALITY", "Note quality or trust"
    UPLOAD = "UPLOAD", "Upload or technical problem"
    ALTERNATIVE = "ALTERNATIVE", "Uses another tool"
    FORGOT = "FORGOT", "Forgot / did not know what to do next"
    UNREACHABLE = "UNREACHABLE", "Did not answer"
    OTHER = "OTHER", "Other"


class CallOutcome(BaseModel):
    """What a user said when the team called them: turns call answers into countable reasons."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="call_outcomes")
    list_key = models.CharField(max_length=32, db_index=True)
    reason = models.CharField(max_length=16, choices=CallReason.choices, db_index=True)
    note = models.TextField(blank=True)
    called_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "created_at"], name="call_outcome_user_idx")]


class ApiErrorEvent(BaseModel):
    """One error a user ran into, by product area, for the admin monitoring page.

    Written by the server (4xx/5xx API responses of signed-in users, and 429/5xx of anyone) and by the
    browser (network failures, replaced responses) so a problem is visible even when the request never
    reached us. Rows are short-lived (see `services.admin.api_errors`).
    """

    SOURCE_SERVER = "server"
    SOURCE_CLIENT = "client"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="api_error_events"
    )
    area = models.CharField(max_length=24, db_index=True)
    severity = models.CharField(max_length=8, db_index=True)  # critical | warning | info
    source = models.CharField(max_length=8, default=SOURCE_SERVER)
    method = models.CharField(max_length=8, blank=True)
    path = models.CharField(max_length=200, blank=True)
    status_code = models.PositiveSmallIntegerField(default=0)
    error_code = models.CharField(max_length=64, blank=True)
    message = models.CharField(max_length=300, blank=True)
    request_id = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["created_at"]),
            models.Index(fields=["area", "created_at"]),
            models.Index(fields=["severity", "created_at"]),
        ]
