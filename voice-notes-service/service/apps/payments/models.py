from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from apps.common.models import BaseModel


class PaymentStatus(models.TextChoices):
    CREATED = "CREATED", "Created"
    REQUESTING = "REQUESTING", "Requesting"
    PENDING = "PENDING", "Pending"
    VERIFYING = "VERIFYING", "Verifying"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"
    CANCELED = "CANCELED", "Canceled"
    EXPIRED = "EXPIRED", "Expired"


class PaymentPurpose(models.TextChoices):
    PLAN_PURCHASE = "PLAN_PURCHASE", "Plan purchase"
    MINUTE_TOPUP = "MINUTE_TOPUP", "Minute top-up"
    WALLET_TOPUP = "WALLET_TOPUP", "Wallet top-up"


class DiscountReservationStatus(models.TextChoices):
    RESERVED = "RESERVED", "Reserved"
    CONSUMED = "CONSUMED", "Consumed"
    RELEASED = "RELEASED", "Released"


class DiscountCode(BaseModel):
    code = models.CharField(max_length=40, unique=True, db_index=True)
    title = models.CharField(max_length=120, blank=True)
    percent = models.PositiveSmallIntegerField()
    is_active = models.BooleanField(default=True, db_index=True)
    max_redemptions = models.PositiveIntegerField(null=True, blank=True)
    redeemed_count = models.PositiveIntegerField(default=0)
    starts_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    # Empty (the default) means unrestricted -- valid for every paid plan,
    # which is the exact behavior every code created before this field
    # existed already has, so adding it needs no data migration.
    applicable_plans = models.ManyToManyField(
        "subscriptions.SubscriptionPlan",
        related_name="discount_codes",
        blank=True,
    )

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["code", "is_active"]),
            models.Index(fields=["is_active", "expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.code}:{self.percent}%"

    def clean(self) -> None:
        super().clean()
        if self.percent < 1 or self.percent > 100:
            raise ValidationError({"percent": "درصد تخفیف باید بین ۱ تا ۱۰۰ باشد."})
        if self.max_redemptions is not None and self.max_redemptions < 1:
            raise ValidationError({"max_redemptions": "تعداد استفاده مجاز باید حداقل ۱ باشد."})


class FirstPurchaseDiscount(BaseModel):
    """One per-user discount window, created exactly once and never extended.

    The window opens when the user's first note is ready (or lazily the first time an
    existing user is evaluated) and is enforced server-side at checkout.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="first_purchase_discount",
    )
    percent = models.PositiveSmallIntegerField()
    starts_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    expiry_recorded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)

    def __str__(self) -> str:
        return f"{self.user_id}:{self.percent}% until {self.expires_at:%Y-%m-%d %H:%M}"


class PaymentRecord(BaseModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="payments",
    )
    subscription_plan = models.ForeignKey(
        "subscriptions.SubscriptionPlan",
        on_delete=models.PROTECT,
        related_name="payments",
    )
    purpose = models.CharField(
        max_length=16,
        choices=PaymentPurpose.choices,
        default=PaymentPurpose.PLAN_PURCHASE,
        db_index=True,
    )
    topup_minutes = models.PositiveIntegerField(default=0)
    # Wallet top-ups: what is credited as paid money and as first-top-up gift (decided at creation).
    wallet_amount_toman = models.PositiveIntegerField(default=0)
    wallet_gift_toman = models.PositiveIntegerField(default=0)
    amount = models.PositiveIntegerField()
    original_amount = models.PositiveIntegerField(default=0)
    public_discount_percent = models.PositiveSmallIntegerField(default=0)
    public_discount_amount = models.PositiveIntegerField(default=0)
    discount_code = models.ForeignKey(
        DiscountCode,
        on_delete=models.SET_NULL,
        related_name="payments",
        null=True,
        blank=True,
    )
    discount_code_text = models.CharField(max_length=40, blank=True)
    discount_percent = models.PositiveSmallIntegerField(default=0)
    discount_amount = models.PositiveIntegerField(default=0)
    first_purchase_discount_percent = models.PositiveSmallIntegerField(default=0)
    first_purchase_discount_amount = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=32,
        choices=PaymentStatus.choices,
        default=PaymentStatus.PENDING,
        db_index=True,
    )
    provider = models.CharField(max_length=32, default="legacy", db_index=True)
    order_id = models.CharField(max_length=64, blank=True, unique=True, null=True)
    gateway_track_id = models.CharField(max_length=128, blank=True, unique=True, null=True)
    gateway_amount_rial = models.PositiveBigIntegerField(default=0)
    gateway_status = models.IntegerField(null=True, blank=True)
    authority = models.CharField(max_length=128, blank=True, unique=True, null=True)
    ref_id = models.CharField(max_length=128, blank=True, unique=True, null=True)
    gateway_response = models.JSONField(default=dict, blank=True)
    payment_url = models.URLField(max_length=1000, blank=True)
    manual_tracking_code = models.CharField(max_length=32, blank=True, unique=True, null=True)
    receipt_file = models.FileField(upload_to="payment_receipts/%Y/%m/", blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="reviewed_payments",
        null=True,
        blank=True,
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.TextField(blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    initiated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    fulfilled_subscription = models.ForeignKey(
        "subscriptions.UserSubscription",
        on_delete=models.SET_NULL,
        related_name="fulfilling_payments",
        null=True,
        blank=True,
    )
    failure_code = models.CharField(max_length=64, blank=True)
    failure_message = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["subscription_plan", "status"]),
            models.Index(fields=["created_at"]),
            models.Index(fields=["provider", "status", "expires_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "subscription_plan", "purpose"],
                condition=models.Q(
                    provider="zibal",
                    status__in=["CREATED", "REQUESTING", "PENDING", "VERIFYING"],
                ),
                name="unique_active_zibal_checkout_per_user_plan_purpose",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.subscription_plan_id}:{self.status}"


class DiscountReservation(BaseModel):
    discount_code = models.ForeignKey(DiscountCode, on_delete=models.CASCADE, related_name="reservations")
    payment = models.OneToOneField(PaymentRecord, on_delete=models.CASCADE, related_name="discount_reservation")
    status = models.CharField(
        max_length=16,
        choices=DiscountReservationStatus.choices,
        default=DiscountReservationStatus.RESERVED,
        db_index=True,
    )
    expires_at = models.DateTimeField(db_index=True)
    consumed_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["discount_code", "status", "expires_at"])]
