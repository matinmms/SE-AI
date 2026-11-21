from __future__ import annotations

import re


class InvalidPhoneNumberError(ValueError):
    pass


_NON_DIGIT_RE = re.compile(r"\D+")
_IRANIAN_MOBILE_RE = re.compile(r"^09\d{9}$")


def normalize_iranian_mobile_number(phone_number: str) -> str:
    raw_value = (phone_number or "").strip()
    digits = _NON_DIGIT_RE.sub("", raw_value)

    if digits.startswith("0098"):
        digits = digits[4:]
    elif digits.startswith("98"):
        digits = digits[2:]

    if digits.startswith("9") and len(digits) == 10:
        digits = f"0{digits}"

    if not _IRANIAN_MOBILE_RE.fullmatch(digits):
        raise InvalidPhoneNumberError("شماره موبایل وارد شده معتبر نیست.")

    return digits


def mask_phone_number(phone_number: str) -> str:
    normalized = normalize_iranian_mobile_number(phone_number)
    return f"{normalized[:4]}***{normalized[-4:]}"
