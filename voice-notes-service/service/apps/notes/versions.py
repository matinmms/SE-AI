"""Other versions of a finished note: the same class written in another style, on request.

A wallet user pays for it from the class length (see note_styles.version_price_toman); the
charge is taken when the request is accepted and refunded if the version fails. Staff and
subscription users are not charged.
"""
from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.notes.models import GeneratedNote, NoteGenerationStatus
from apps.notes.note_styles import (
    NOTE_STYLES,
    alt_variant,
    is_alt_variant,
    normalize_style,
    style_choice_enabled_for,
    style_of_note,
    version_price_toman,
)

REF_TYPE = "note_version"


def _charge_key(lecture: Any, style: str) -> str:
    return f"note-version:{lecture.id}:{style}"


def _main_note(lecture: Any) -> GeneratedNote | None:
    from apps.notes.generation import is_comparison_note_variant

    for note in lecture.generated_notes.filter(deleted_at__isnull=True).order_by("created_at"):
        if not is_comparison_note_variant(note.variant):
            return note
    return None


def _lecture_minutes(lecture: Any) -> int:
    from apps.wallet import pricing

    seconds = float(getattr(lecture, "duration_seconds", 0) or 0)
    return pricing.minutes_for_seconds(seconds) if seconds > 0 else 0


def _charges_user(user: Any) -> bool:
    from apps.wallet.services import wallet_active_for

    return not getattr(user, "is_staff", False) and wallet_active_for(user)


def versions_payload(*, note: GeneratedNote) -> dict[str, Any]:
    """Every style of this class: which exist, which are being made, and what a new one costs."""
    lecture = note.lecture
    main = _main_note(lecture)
    minutes = _lecture_minutes(lecture)
    charges = _charges_user(note.user)
    by_style: dict[str, GeneratedNote] = {}
    if main is not None:
        by_style[style_of_note(main)] = main
    for other in lecture.generated_notes.filter(deleted_at__isnull=True).order_by("created_at"):
        if is_alt_variant(other.variant):
            by_style.setdefault(style_of_note(other), other)
    pending = (lecture.processing_metadata or {}).get("note_versions") or {}
    versions = []
    for style in NOTE_STYLES:
        row = by_style.get(style)
        status = row.status if row is not None else ""
        if row is None and style in pending:
            status = NoteGenerationStatus.QUEUED
        versions.append(
            {
                "style": style,
                "note_id": str(row.id) if row is not None else "",
                "status": status,
                "is_main": row is not None and main is not None and row.id == main.id,
                "price_toman": version_price_toman(style=style, minutes=minutes) if charges and row is None else 0,
            }
        )
    return {
        "enabled": style_choice_enabled_for(note.user),
        "current_style": style_of_note(note),
        "minutes": minutes,
        "charged": charges,
        "versions": versions,
    }


class VersionRequestError(Exception):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def request_version(*, note: GeneratedNote, style: str) -> dict[str, Any]:
    """Queue the class in another style (idempotent). Returns the new versions payload."""
    user = note.user
    style = normalize_style(style)
    if not style or not style_choice_enabled_for(user):
        raise VersionRequestError("STYLE_UNAVAILABLE", "این نسخه برای حساب شما فعال نیست.")
    lecture = note.lecture
    payload = versions_payload(note=note)
    current = next(item for item in payload["versions"] if item["style"] == style)
    if (current["note_id"] and current["status"] != NoteGenerationStatus.FAILED) or current["status"] == NoteGenerationStatus.QUEUED:
        return payload  # already made or on its way
    price = int(current["price_toman"] or 0)
    with transaction.atomic():
        if price > 0:
            from apps.wallet.models import WalletTxKind
            from apps.wallet.services import available_toman, debit

            if available_toman(user) < price:
                raise VersionRequestError(
                    "INSUFFICIENT_WALLET_BALANCE",
                    "موجودی حسابت برای ساخت این نسخه کافی نیست؛ اول حسابت را شارژ کن.",
                    details={"wallet": True, "available_toman": available_toman(user), "price_toman": price},
                )
            # A failed earlier try was refunded; a new try is a new charge.
            attempt = int(((lecture.processing_metadata or {}).get("note_version_attempts") or {}).get(style, 0)) + 1
            debit(
                user=user, amount=price, key=f"{_charge_key(lecture, style)}:{attempt}", kind=WalletTxKind.ADDON_CHARGE,
                ref_type=REF_TYPE, ref_id=str(lecture.id), metadata={"style": style, "minutes": payload["minutes"]},
            )
        else:
            attempt = int(((lecture.processing_metadata or {}).get("note_version_attempts") or {}).get(style, 0)) + 1
        metadata = dict(lecture.processing_metadata or {})
        metadata["note_versions"] = {**(metadata.get("note_versions") or {}), style: {"requested_at": timezone.now().isoformat(), "price_toman": price}}
        metadata["note_version_attempts"] = {**(metadata.get("note_version_attempts") or {}), style: attempt}
        lecture.processing_metadata = metadata
        lecture.save(update_fields=["processing_metadata", "updated_at"])
        GeneratedNote.objects.filter(lecture=lecture, variant__endswith=f"-alt-{style}", status=NoteGenerationStatus.FAILED).delete()

        from apps.notes.tasks import generate_note_version_task

        transaction.on_commit(lambda: generate_note_version_task.delay(str(lecture.id), style))
    return versions_payload(note=note)


def clear_pending(*, lecture: Any, style: str) -> None:
    lecture.refresh_from_db()
    metadata = dict(lecture.processing_metadata or {})
    pending = dict(metadata.get("note_versions") or {})
    if pending.pop(style, None) is not None:
        metadata["note_versions"] = pending
        lecture.processing_metadata = metadata
        lecture.save(update_fields=["processing_metadata", "updated_at"])


def refund_version_charge(*, lecture: Any, style: str) -> None:
    """Give back what the failed version cost (once per attempt)."""
    from apps.wallet.models import WalletTransaction, WalletTxKind
    from apps.wallet.services import credit_paid

    attempt = int(((lecture.processing_metadata or {}).get("note_version_attempts") or {}).get(style, 0))
    key = f"{_charge_key(lecture, style)}:{attempt}"
    charge = WalletTransaction.objects.filter(idempotency_key=key).first()
    if charge is not None and charge.amount_toman < 0:
        with transaction.atomic():
            credit_paid(
                user=lecture.user, amount=-int(charge.amount_toman), key=f"{key}:refund", kind=WalletTxKind.REFUND,
                ref_type=REF_TYPE, ref_id=str(lecture.id), metadata={"style": style},
            )
    clear_pending(lecture=lecture, style=style)


def base_variant_for(note: GeneratedNote) -> str:
    variant = str(note.variant or "")
    return variant.split("-alt-")[0].replace("-compare-v2", "")


__all__ = ["versions_payload", "request_version", "refund_version_charge", "clear_pending", "VersionRequestError", "alt_variant"]
