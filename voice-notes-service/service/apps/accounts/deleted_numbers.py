"""Remember phone numbers of deleted accounts so re-registering does not grant the free minutes again."""
from __future__ import annotations

import hashlib
import hmac
import re

from django.conf import settings
from django.utils import timezone

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def phone_fingerprint(phone_number: str | None) -> str:
    """Keyed hash of the number's last ten digits, so 0912..., +98912... and Persian digits all match."""
    digits = re.sub(r"\D", "", str(phone_number or "").translate(_PERSIAN_DIGITS))[-10:]
    if len(digits) < 10:
        return ""
    return hmac.new(settings.SECRET_KEY.encode(), digits.encode(), hashlib.sha256).hexdigest()


def is_fresh_test_number(phone_number: str | None) -> bool:
    """The founder's test numbers (FRESH_ACCOUNT_PHONE_NUMBERS): every sign-up is a brand-new user."""
    fingerprint = phone_fingerprint(phone_number)
    return bool(fingerprint) and any(
        phone_fingerprint(number) == fingerprint for number in getattr(settings, "FRESH_ACCOUNT_PHONE_NUMBERS", [])
    )


def remember_deleted_phone(phone_number: str | None) -> None:
    from apps.accounts.models import DeletedPhoneNumber

    fingerprint = phone_fingerprint(phone_number)
    if not fingerprint or is_fresh_test_number(phone_number):
        return
    now = timezone.now()
    record, created = DeletedPhoneNumber.objects.get_or_create(
        phone_hash=fingerprint, defaults={"last_deleted_at": now}
    )
    if not created:
        record.deleted_count += 1
        record.last_deleted_at = now
        record.save(update_fields=["deleted_count", "last_deleted_at", "updated_at"])


def phone_was_deleted(phone_number: str | None) -> bool:
    from apps.accounts.models import DeletedPhoneNumber

    fingerprint = phone_fingerprint(phone_number)
    if not fingerprint or is_fresh_test_number(phone_number):
        return False
    return DeletedPhoneNumber.objects.filter(phone_hash=fingerprint).exists()
