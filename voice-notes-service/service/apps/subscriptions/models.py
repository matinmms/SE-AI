from __future__ import annotations

from datetime import timedelta

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.common.models import BaseModel


class PlanKey(models.TextChoices):
    FREE = "FREE", "Free"
    PLUS = "PLUS", "Plus"
    PRO = "PRO", "Pro"
    MAX = "MAX", "Max"


class SubscriptionPlan(BaseModel):
    key = models.CharField(max_length=32, choices=PlanKey.choices, unique=True, db_index=True)
    name = models.CharField(max_length=64)
    subtitle = models.CharField(max_length=64, blank=True)
    monthly_minutes = models.PositiveIntegerField()
    duration_months = models.PositiveSmallIntegerField(default=1)
    price_toman = models.PositiveIntegerField()
    original_price_toman = models.PositiveIntegerField(default=0)
    discount_percent = models.PositiveSmallIntegerField(default=0)
    is_active = models.BooleanField(default=True, db_index=True)
    is_featured = models.BooleanField(default=False)
    badge_text = models.CharField(max_length=32, blank=True)
    benefit_line = models.CharField(max_length=160, blank=True)
    display_order = models.PositiveSmallIntegerField(default=0)
    public_discount_percent = models.PositiveSmallIntegerField(default=0)
    public_discount_is_active = models.BooleanField(default=False, db_index=True)
    public_discount_starts_at = models.DateTimeField(null=True, blank=True)
    public_discount_expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("display_order", "price_toman", "created_at")
        indexes = [
            models.Index(fields=["is_active", "key"]),
        ]

    def __str__(self) -> str:
        return self.name

    def clean(self) -> None:
        super().clean()
        if self.key == PlanKey.FREE and self.price_toman != 0:
            raise ValidationError({"price_toman": "قیمت پلن رایگان باید صفر باشد."})
        if self.key != PlanKey.FREE and self.price_toman <= 0:
            raise ValidationError({"price_toman": "قیمت پلن‌های پولی باید بیشتر از صفر باشد."})
        if self.public_discount_percent > 100:
            raise ValidationError({"public_discount_percent": "درصد تخفیف عمومی باید بین ۰ تا ۱۰۰ باشد."})
        if self.key == PlanKey.FREE and (self.public_discount_percent > 0 or self.public_discount_is_active):
            raise ValidationError({"public_discount_percent": "پلن رایگان نمی‌تواند تخفیف عمومی داشته باشد."})
        if (
            self.public_discount_starts_at
            and self.public_discount_expires_at
            and self.public_discount_starts_at >= self.public_discount_expires_at
        ):
            raise ValidationError({"public_discount_expires_at": "تاریخ پایان باید بعد از تاریخ شروع باشد."})
        if self.discount_percent > 100:
            raise ValidationError({"discount_percent": "درصد تخفیف نمایشی باید بین ۰ تا ۱۰۰ باشد."})
        if self.original_price_toman and self.original_price_toman < self.price_toman:
            raise ValidationError(
                {"original_price_toman": "قیمت اصلی نمی‌تواند کمتر از قیمت نهایی باشد."}
            )
        if self.duration_months < 1:
            raise ValidationError({"duration_months": "مدت پلن باید حداقل یک ماه باشد."})

    def expiry_from(self, *, started_at):
        """Calendar-aware expiry: a 1-month plan started on the 31st lands on
        the target month's real last day, not 30 days later; a 3-month plan
        adds three real calendar months. Never a naive day-count fallback.
        """
        return started_at + relativedelta(months=self.duration_months)

    def active_public_discount_percent(self, *, now=None) -> int:
        now = now or timezone.now()
        if self.key == PlanKey.FREE or not self.public_discount_is_active or self.public_discount_percent <= 0:
            return 0
        if self.public_discount_starts_at and self.public_discount_starts_at > now:
            return 0
        if self.public_discount_expires_at and self.public_discount_expires_at <= now:
            return 0
        return self.public_discount_percent


class UserSubscription(BaseModel):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscriptions",
    )
    plan = models.ForeignKey(
        "subscriptions.SubscriptionPlan",
        on_delete=models.PROTECT,
        related_name="subscriptions",
    )
    started_at = models.DateTimeField(db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    remaining_minutes = models.PositiveIntegerField()
    reserved_minutes = models.PositiveIntegerField(default=0)
    used_minutes = models.PositiveIntegerField(default=0)
    bonus_minutes = models.PositiveIntegerField(default=0)
    # Plans handed out by staff have no payment behind them, so the payment-based
    # entitlement sweep must leave them alone until their own expires_at passes.
    granted_by_admin = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(is_active=True),
                name="unique_active_subscription_per_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["expires_at", "is_active"]),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.plan.key}"

    EXPIRY_WARNING_DAYS = 3

    @property
    def days_until_expiry(self) -> int:
        remaining = self.expires_at - timezone.now()
        return max(0, remaining.days + (1 if remaining.seconds > 0 else 0))

    @property
    def is_expiring_soon(self) -> bool:
        if self.plan.key == PlanKey.FREE:
            return False
        remaining = self.expires_at - timezone.now()
        return timedelta(0) <= remaining <= timedelta(days=self.EXPIRY_WARNING_DAYS)

    @property
    def available_minutes(self) -> int:
        return max(0, (self.remaining_minutes + self.bonus_minutes) - self.reserved_minutes)

    @classmethod
    def default_expiry(cls, *, started_at=None):
        started = started_at or timezone.now()
        return started + timedelta(days=30)
