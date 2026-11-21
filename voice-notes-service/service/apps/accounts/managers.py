from __future__ import annotations

from django.contrib.auth.base_user import BaseUserManager

from services.auth.phone import normalize_iranian_mobile_number


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def _create_user(
        self,
        phone_number: str | None = None,
        password: str | None = None,
        *,
        email: str | None = None,
        **extra_fields,
    ):
        if not phone_number and not email:
            raise ValueError("A phone number or email address must be provided.")
        normalized_phone_number = normalize_iranian_mobile_number(phone_number) if phone_number else None
        normalized_email = self.normalize_email(email).lower() if email else None
        user = self.model(phone_number=normalized_phone_number, email=normalized_email, **extra_fields)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save(using=self._db)
        from services.subscriptions.lifecycle import SubscriptionLifecycleService
        from apps.subscriptions.models import PlanKey

        SubscriptionLifecycleService.create_subscription(user=user, plan_key=PlanKey.FREE)
        return user

    def create_user(self, phone_number: str | None = None, password: str | None = None, **extra_fields):
        email = extra_fields.pop("email", None)
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        extra_fields.setdefault("is_active", True)
        return self._create_user(phone_number, password, email=email, **extra_fields)

    def create_superuser(self, phone_number: str | None = None, password: str | None = None, **extra_fields):
        email = extra_fields.pop("email", None)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        if not phone_number and not email:
            raise ValueError("Superuser must have a phone number or email.")
        return self._create_user(phone_number, password, email=email, **extra_fields)
