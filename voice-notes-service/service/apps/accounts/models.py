from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models

from apps.accounts.managers import UserManager
from apps.common.models import BaseModel


class SubscriptionPlan(models.TextChoices):
    FREE = "FREE", "Free"
    PRO = "PRO", "Pro"
    PREMIUM = "PREMIUM", "Premium"
    MAX = "MAX", "Max"


SYSTEM_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class User(AbstractBaseUser, PermissionsMixin, BaseModel):
    phone_number = models.CharField(max_length=11, unique=True, db_index=True, null=True, blank=True)
    email = models.EmailField(unique=True, db_index=True, null=True, blank=True)
    full_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    subscription_plan = models.CharField(
        max_length=32,
        choices=SubscriptionPlan.choices,
        default=SubscriptionPlan.FREE,
        db_index=True,
    )
    remaining_minutes = models.PositiveIntegerField(default=180)
    referral_code = models.CharField(max_length=12, unique=True, null=True, blank=True)
    # Founder-controlled rollout flag for the note accuracy check (see services/note_review).
    accuracy_check_enabled = models.BooleanField(default=False, db_index=True)
    # The note style used without asking ("" = ask every time): explained / lecturer / transcript.
    note_style_preference = models.CharField(max_length=16, blank=True, default="")
    # When the user first saw the editor's guided tour; it is shown once per account, on any device.
    editor_tour_seen_at = models.DateTimeField(null=True, blank=True)
    referred_by = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referred_users",
    )

    USERNAME_FIELD = "phone_number"
    REQUIRED_FIELDS: list[str] = ["full_name"]

    objects = UserManager()

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "user"
        verbose_name_plural = "users"

    def __str__(self) -> str:
        return self.phone_number or self.email or str(self.id)

    def save(self, *args, **kwargs):
        if self.email:
            self.email = self.email.lower().strip()
        if self.phone_number:
            self.phone_number = self.phone_number.strip()
        self.full_name = self.full_name.strip()
        super().save(*args, **kwargs)


class SignupAttribution(BaseModel):
    """First-touch acquisition data captured on the device before signup, plus the optional self-reported source."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="signup_attribution")
    utm_source = models.CharField(max_length=80, blank=True, default="")
    utm_medium = models.CharField(max_length=80, blank=True, default="")
    utm_campaign = models.CharField(max_length=120, blank=True, default="")
    utm_content = models.CharField(max_length=120, blank=True, default="")
    utm_term = models.CharField(max_length=120, blank=True, default="")
    ref = models.CharField(max_length=80, blank=True, default="")
    landing_path = models.CharField(max_length=200, blank=True, default="")
    survey_answer = models.CharField(max_length=40, blank=True, default="")
    survey_other_text = models.CharField(max_length=120, blank=True, default="")
    survey_answered_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user_id}: {self.utm_source or self.ref or 'direct'}"


class DeletedPhoneNumber(BaseModel):
    """A phone number whose account was deleted. It survives the deletion so deleting and
    re-registering cannot be used to collect the free minutes again. Only a keyed hash is stored."""

    phone_hash = models.CharField(max_length=64, unique=True)
    deleted_count = models.PositiveIntegerField(default=1)
    last_deleted_at = models.DateTimeField()

    def __str__(self) -> str:
        return f"{self.phone_hash[:8]} x{self.deleted_count}"


def get_system_user_pk():
    return SYSTEM_USER_ID
