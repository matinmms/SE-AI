"""Wallet operations. Every balance change writes its ledger row in the same transaction and is
idempotent by key, so retries, double webhooks and re-run tasks never charge or credit twice."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.wallet import pricing
from apps.wallet.models import (
    OfferState,
    TopupOffer,
    WalletAccount,
    WalletGiftLot,
    WalletHold,
    WalletHoldStatus,
    WalletTransaction,
    WalletTxKind,
)

logger = logging.getLogger("tesanote.wallet")


class WalletError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


# ------------------------------------------------------------------ gate


def rollout_bucket(user: Any) -> int:
    """A stable number 0-99 per user, so the same people stay in the wallet group as the dial grows."""
    digest = hashlib.sha256(f"wallet-rollout:{getattr(user, 'id', '')}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


def _keeps_paid_minutes(user: Any) -> bool:
    """A subscriber with a live paid plan and minutes left keeps using minutes; once those run out
    or the plan ends they move to the wallet like everyone else."""
    from apps.subscriptions.models import PlanKey, UserSubscription

    subscription = (
        UserSubscription.objects.filter(user=user, is_active=True, expires_at__gt=timezone.now())
        .select_related("plan")
        .first()
    )
    return bool(subscription and subscription.plan.key != PlanKey.FREE and subscription.available_minutes > 0)


def wallet_active_for(user: Any) -> bool:
    """Wallet billing replaces minutes and plans only for the users it has been switched on for.

    Order of the rules:
    - the master switch, and a signed-in user;
    - the chosen phone numbers (the founder's test accounts) are always in;
    - anyone who already has wallet money stays in, so lowering the dial never strands a balance;
    - otherwise in, if `WALLET_BILLING_FOR_ALL` or the user's rollout bucket is under
      `WALLET_BILLING_ROLLOUT_PERCENT`, unless they still have live paid minutes.
    """
    if not getattr(user, "is_authenticated", False) or not settings.WALLET_BILLING_ENABLED:
        return False
    if str(getattr(user, "phone_number", "") or "") in settings.WALLET_BILLING_PHONE_NUMBERS:
        return True
    has_record = getattr(user, "pk", None) is not None  # callers may pass a lightweight stand-in
    if has_record and WalletAccount.objects.filter(user=user).exists() and WalletTransaction.objects.filter(user=user).exists():
        return True
    selected = settings.WALLET_BILLING_FOR_ALL or rollout_bucket(user) < int(settings.WALLET_BILLING_ROLLOUT_PERCENT)
    if not selected:
        return False
    return not (has_record and _keeps_paid_minutes(user))


# ------------------------------------------------------------------ accounts and ledger


def _locked_account(user_id: Any) -> WalletAccount:
    try:
        WalletAccount.objects.get_or_create(user_id=user_id)
    except IntegrityError:
        pass
    return WalletAccount.objects.select_for_update().get(user_id=user_id)


def balance_summary(user: Any) -> dict[str, int]:
    account = WalletAccount.objects.filter(user=user).first()
    if account is None:
        return {"paid_toman": 0, "gift_toman": 0, "held_toman": 0, "available_toman": 0}
    return {
        "paid_toman": int(account.paid_toman),
        "gift_toman": int(account.gift_toman),
        "held_toman": int(account.held_toman),
        "available_toman": account.available_toman,
    }


def available_toman(user: Any) -> int:
    return balance_summary(user)["available_toman"]


def _existing(key: str) -> WalletTransaction | None:
    return WalletTransaction.objects.filter(idempotency_key=key).first()


@transaction.atomic
def credit_paid(*, user: Any, amount: int, key: str, kind: str = WalletTxKind.TOPUP, ref_type: str = "", ref_id: str = "", metadata: dict | None = None) -> WalletTransaction:
    if amount <= 0:
        raise WalletError("INVALID_AMOUNT", "Credit must be positive.")
    existing = _existing(key)
    if existing is not None:
        return existing
    account = _locked_account(user.id)
    account.paid_toman += amount
    account.save(update_fields=["paid_toman", "updated_at"])
    return WalletTransaction.objects.create(
        user=user, kind=kind, amount_toman=amount, paid_part=amount, idempotency_key=key, ref_type=ref_type,
        ref_id=ref_id, balance_after=account.paid_toman + account.gift_toman, metadata=metadata or {},
    )


@transaction.atomic
def credit_gift(*, user: Any, amount: int, key: str, expires_at, source: str = "first_topup", ref_type: str = "", ref_id: str = "") -> WalletTransaction:
    if amount <= 0:
        raise WalletError("INVALID_AMOUNT", "Gift must be positive.")
    existing = _existing(key)
    if existing is not None:
        return existing
    account = _locked_account(user.id)
    WalletGiftLot.objects.create(user=user, amount_toman=amount, remaining_toman=amount, expires_at=expires_at, source=source)
    account.gift_toman += amount
    account.save(update_fields=["gift_toman", "updated_at"])
    return WalletTransaction.objects.create(
        user=user, kind=WalletTxKind.GIFT, amount_toman=amount, gift_part=amount, idempotency_key=key,
        ref_type=ref_type, ref_id=ref_id, balance_after=account.paid_toman + account.gift_toman,
        metadata={"expires_at": expires_at.isoformat(), "source": source},
    )


@transaction.atomic
def debit(*, user: Any, amount: int, key: str, kind: str, ref_type: str = "", ref_id: str = "", metadata: dict | None = None) -> WalletTransaction | None:
    """Spend gift lots first (soonest expiry), then the paid balance, which may go below zero."""
    if amount <= 0:
        return None
    existing = _existing(key)
    if existing is not None:
        return existing
    account = _locked_account(user.id)
    now = timezone.now()
    needed = int(amount)
    gift_used = 0
    for lot in WalletGiftLot.objects.select_for_update().filter(user=user, remaining_toman__gt=0, expires_at__gt=now).order_by("expires_at"):
        take = min(lot.remaining_toman, needed)
        lot.remaining_toman -= take
        lot.save(update_fields=["remaining_toman", "updated_at"])
        gift_used += take
        needed -= take
        if needed == 0:
            break
    paid_used = needed
    account.gift_toman -= gift_used
    account.paid_toman -= paid_used
    account.save(update_fields=["gift_toman", "paid_toman", "updated_at"])
    return WalletTransaction.objects.create(
        user=user, kind=kind, amount_toman=-int(amount), paid_part=-paid_used, gift_part=-gift_used,
        idempotency_key=key, ref_type=ref_type, ref_id=ref_id,
        balance_after=account.paid_toman + account.gift_toman, metadata=metadata or {},
    )


def expire_gifts(*, now=None) -> int:
    """Remove gift credit that passed its expiry. Idempotent per lot."""
    now = now or timezone.now()
    expired = 0
    for lot_id in list(WalletGiftLot.objects.filter(remaining_toman__gt=0, expires_at__lte=now).values_list("id", flat=True)):
        with transaction.atomic():
            lot = WalletGiftLot.objects.select_for_update().get(id=lot_id)
            if lot.remaining_toman <= 0:
                continue
            account = _locked_account(lot.user_id)
            lost = int(lot.remaining_toman)
            lot.remaining_toman = 0
            lot.save(update_fields=["remaining_toman", "updated_at"])
            account.gift_toman = max(0, account.gift_toman - lost)
            account.save(update_fields=["gift_toman", "updated_at"])
            WalletTransaction.objects.get_or_create(
                idempotency_key=f"gift-expired:{lot.id}",
                defaults={
                    "user_id": lot.user_id, "kind": WalletTxKind.GIFT_EXPIRED, "amount_toman": -lost, "gift_part": -lost,
                    "ref_type": "gift_lot", "ref_id": str(lot.id), "balance_after": account.paid_toman + account.gift_toman,
                },
            )
            expired += 1
    return expired


# ------------------------------------------------------------------ holds (a note being processed)


@transaction.atomic
def place_hold(*, user: Any, lecture: Any, estimate_toman: int) -> WalletHold:
    """Set money aside for a lecture. Idempotent per lecture."""
    existing = WalletHold.objects.filter(lecture=lecture).first()
    if existing is not None:
        return existing
    account = _locked_account(user.id)
    amount = min(pricing.hold_for_estimate(estimate_toman), max(estimate_toman, account.available_toman))
    hold = WalletHold.objects.create(user=user, lecture=lecture, amount_toman=amount, estimate_toman=estimate_toman)
    account.held_toman += amount
    account.save(update_fields=["held_toman", "updated_at"])
    return hold


@transaction.atomic
def settle_hold(*, lecture: Any, minutes: int) -> WalletHold | None:
    """The note is done: charge its real cost plus the per-minute profit and free the hold."""
    hold = WalletHold.objects.select_for_update().filter(lecture=lecture).first()
    if hold is None or hold.status != WalletHoldStatus.HELD:
        return hold
    account = _locked_account(hold.user_id)
    breakdown = pricing.note_cost_breakdown(lecture, minutes=minutes)
    account.held_toman = max(0, account.held_toman - hold.amount_toman)
    account.save(update_fields=["held_toman", "updated_at"])
    debit(
        user=hold.user, amount=breakdown["total_toman"], key=f"note:{lecture.id}", kind=WalletTxKind.NOTE_CHARGE,
        ref_type="lecture", ref_id=str(lecture.id), metadata=breakdown,
    )
    hold.status = WalletHoldStatus.SETTLED
    hold.charged_toman = breakdown["total_toman"]
    hold.cost_breakdown = breakdown
    hold.save(update_fields=["status", "charged_toman", "cost_breakdown", "updated_at"])
    return hold


@transaction.atomic
def release_hold(*, lecture: Any) -> WalletHold | None:
    """The note failed: nothing is charged."""
    hold = WalletHold.objects.select_for_update().filter(lecture=lecture).first()
    if hold is None or hold.status != WalletHoldStatus.HELD:
        return hold
    account = _locked_account(hold.user_id)
    account.held_toman = max(0, account.held_toman - hold.amount_toman)
    account.save(update_fields=["held_toman", "updated_at"])
    hold.status = WalletHoldStatus.RELEASED
    hold.save(update_fields=["status", "updated_at"])
    return hold


# ------------------------------------------------------------------ add-ons (summary, quiz, chat)


def addon_allowed(user: Any) -> bool:
    """Wallet users need a little balance to start an add-on; others are unaffected."""
    if not wallet_active_for(user):
        return True
    return available_toman(user) >= int(settings.WALLET_ADDON_MIN_BALANCE_TOMAN)


# ------------------------------------------------------------------ add-ons on the first (free) note


def first_note_lecture_id(user: Any) -> Any:
    """The lecture of the user's very first finished note (deleted notes still count, so deleting one
    cannot hand out a second free note)."""
    from apps.notes.models import GeneratedNote, NoteGenerationStatus

    return (
        GeneratedNote.all_objects.filter(user=user, status=NoteGenerationStatus.COMPLETED)
        .order_by("created_at")
        .values_list("lecture_id", flat=True)
        .first()
    )


def free_chat_messages_limit() -> int:
    return max(0, int(settings.WALLET_FIRST_NOTE_FREE_CHAT_MESSAGES))


def _chat_messages_used(user: Any, lecture_id: Any, *, until=None) -> int:
    from apps.notes.models import NoteChatMessage, NoteChatMessageRole

    queryset = NoteChatMessage.objects.filter(user=user, note__lecture_id=lecture_id, role=NoteChatMessageRole.USER)
    if until is not None:
        queryset = queryset.filter(created_at__lte=until)
    return queryset.count()


def addon_free_on_first_note(user: Any, note: Any, feature: str) -> bool:
    """Summary, flashcards and quiz are free on the first note; chat is free for the first few messages."""
    if note is None or not wallet_active_for(user):
        return False
    if note.lecture_id != first_note_lecture_id(user):
        return False
    if feature != "chatbot":
        return True
    return _chat_messages_used(user, note.lecture_id) < free_chat_messages_limit()


def _addon_request_is_free(row: Any) -> bool:
    """Was this provider request made for a free add-on (first note, within the free chat messages)?"""
    lecture_id = getattr(row, "lecture_id", None)
    if not lecture_id or lecture_id != first_note_lecture_id(row.user):
        return False
    if row.feature != "chatbot":
        return True
    # A chat request is free when it belongs to one of the first N messages; the message is saved a
    # moment around the provider call, so look at messages up to a minute after the request.
    return _chat_messages_used(row.user, lecture_id, until=row.requested_at + timedelta(seconds=60)) <= free_chat_messages_limit()


def charge_addon_request(row: Any) -> None:
    """Charge one add-on ledger row at its real cost. Idempotent per provider request."""
    user = getattr(row, "user", None)
    if user is None or not wallet_active_for(user) or row.feature not in pricing.ADDON_FEATURES:
        return
    if _addon_request_is_free(row):
        return  # the first note's add-ons are a gift
    cost = pricing.request_cost_toman(row)
    amount = pricing.addon_charge_toman(cost)
    debit(
        user=user, amount=amount, key=f"addon:{row.provider_request_id}", kind=WalletTxKind.ADDON_CHARGE,
        ref_type="ai_request", ref_id=str(row.id),
        metadata={"feature": row.feature, "model": row.requested_model, "cost_toman": round(cost, 2)},
    )


# ------------------------------------------------------------------ first top-up gift offer


def gift_tiers() -> list[tuple[int, int]]:
    tiers: list[tuple[int, int]] = []
    for part in str(settings.WALLET_GIFT_TIERS).split(","):
        if ":" in part:
            amount, percent = part.split(":", 1)
            try:
                tiers.append((int(amount), int(percent)))
            except ValueError:
                continue
    return sorted(tiers)


def gift_for_amount(amount: int) -> int:
    """Gift for a top-up: the percent of the highest tier the amount reaches (0 below the first tier)."""
    percent = 0
    for tier_amount, tier_percent in gift_tiers():
        if amount >= tier_amount:
            percent = tier_percent
    return int(amount * percent / 100)


def has_paid_topup(user: Any) -> bool:
    return WalletTransaction.objects.filter(user=user, kind=WalletTxKind.TOPUP).exists()


def _has_completed_note(user: Any) -> bool:
    from apps.notes.models import GeneratedNote, NoteGenerationStatus

    return GeneratedNote.objects.filter(user=user, status=NoteGenerationStatus.COMPLETED).exists()


def offer_state(user: Any, *, now=None) -> dict[str, Any]:
    """What the user may be offered right now. The window is created once and never extended."""
    now = now or timezone.now()
    offer = TopupOffer.objects.filter(user=user).first()
    tiers = [{"amount_toman": a, "percent": p, "gift_toman": int(a * p / 100)} for a, p in gift_tiers()]
    base = {"tiers": tiers, "gift_expiry_days": settings.WALLET_GIFT_EXPIRY_DAYS, "window_minutes": settings.WALLET_OFFER_MINUTES}
    if has_paid_topup(user):
        return {**base, "state": "none", "active": False}
    if offer is None and not _has_completed_note(user):
        # The gift belongs to the moment after the first note: before that there is nothing to start.
        return {**base, "state": "locked", "active": False}
    if offer is None:
        return {**base, "state": "available", "active": False}
    if offer.state == OfferState.STARTED and offer.expires_at > now:
        return {**base, "state": "active", "active": True, "expires_at": offer.expires_at.isoformat(),
                "seconds_remaining": int((offer.expires_at - now).total_seconds()), "server_now": now.isoformat()}
    if offer.state == OfferState.STARTED:
        return {**base, "state": "expired", "active": False}
    return {**base, "state": offer.state.lower(), "active": False}


def offer_is_active(user: Any, *, now=None) -> bool:
    return bool(offer_state(user, now=now).get("active"))


def start_offer(user: Any, *, now=None) -> dict[str, Any]:
    """Start the 15-minute window once per user; a refresh or a second call changes nothing."""
    now = now or timezone.now()
    if has_paid_topup(user) or not _has_completed_note(user):
        return offer_state(user, now=now)
    try:
        with transaction.atomic():
            TopupOffer.objects.get_or_create(
                user=user, defaults={"started_at": now, "expires_at": now + timedelta(minutes=settings.WALLET_OFFER_MINUTES)}
            )
    except IntegrityError:
        pass
    return offer_state(user, now=now)


def skip_offer(user: Any, *, now=None) -> dict[str, Any]:
    """The user declined: the gift is gone for good (the dialog says so before they confirm)."""
    now = now or timezone.now()
    offer, _ = TopupOffer.objects.get_or_create(
        user=user, defaults={"started_at": now, "expires_at": now + timedelta(minutes=settings.WALLET_OFFER_MINUTES)}
    )
    if offer.state == OfferState.STARTED:
        offer.state = OfferState.SKIPPED
        offer.skipped_at = now
        offer.save(update_fields=["state", "skipped_at", "updated_at"])
    return offer_state(user, now=now)


# ------------------------------------------------------------------ leftover free minutes -> gift


def _is_legacy_account(user: Any) -> bool:
    """Created before the wallet went live (WALLET_LEGACY_CUTOFF). Unparseable or missing = legacy."""
    from datetime import datetime

    created = getattr(user, "created_at", None)
    try:
        cutoff = datetime.fromisoformat(str(settings.WALLET_LEGACY_CUTOFF))
    except (TypeError, ValueError):
        return True
    if created is None:
        return True
    if timezone.is_naive(cutoff):
        cutoff = timezone.make_aware(cutoff, timezone.utc)
    return created < cutoff


def convert_legacy_free_minutes(user: Any, *, now=None) -> int:
    """One time per user: unused free minutes of a user who has already made a note become gift credit.

    The minutes are worth `per-minute estimate` toman each, the gift expires in
    `WALLET_LEGACY_GIFT_EXPIRY_DAYS` days and the free minutes are zeroed, so nothing is counted
    twice. A user who has not made their first note yet keeps the free first note instead.
    Returns the gift in toman (0 when nothing was converted). Idempotent per user.
    """
    from apps.subscriptions.models import PlanKey, UserSubscription

    now = now or timezone.now()
    key = f"legacy-free-minutes:{user.id}"
    if _existing(key) is not None:
        return 0
    with transaction.atomic():
        subscription = (
            UserSubscription.objects.select_for_update().select_related("plan").filter(user=user, is_active=True).first()
        )
        if subscription is None or subscription.plan.key != PlanKey.FREE:
            return 0
        if subscription.used_minutes <= 0 or subscription.reserved_minutes > 0:
            return 0
        minutes = int(subscription.remaining_minutes)
        if not _is_legacy_account(user):
            # A new account's free allowance is the free first note, which is now done: nothing is
            # left over and nothing becomes credit, so from here on the user pays from the wallet.
            if minutes > 0:
                subscription.remaining_minutes = 0
                subscription.save(update_fields=["remaining_minutes", "updated_at"])
                logger.info("new_account_free_minutes_cleared", extra={"user_id": str(user.id), "minutes": minutes})
            return 0
        if minutes < max(1, int(settings.FIRST_NOTE_FULL_MIN_BALANCE_MINUTES)):
            return 0
        amount = pricing.round_down(minutes * pricing.per_minute_estimate_toman(), 100)
        if amount <= 0:
            return 0
        credit_gift(
            user=user, amount=amount, key=key, source="legacy_free_minutes", ref_type="subscription", ref_id=str(subscription.id),
            expires_at=now + timedelta(days=settings.WALLET_LEGACY_GIFT_EXPIRY_DAYS),
        )
        subscription.remaining_minutes = 0
        subscription.save(update_fields=["remaining_minutes", "updated_at"])
        logger.info("legacy_free_minutes_converted", extra={"user_id": str(user.id), "minutes": minutes, "gift_toman": amount})
        return amount


# ------------------------------------------------------------------ the summary the frontend shows


def first_note_state(user: Any) -> str:
    """Where the user is with the free first note: unused / in_progress / used."""
    from apps.lectures.models import Lecture
    from apps.notes.models import GeneratedNote, NoteGenerationStatus

    if GeneratedNote.objects.filter(user=user, status=NoteGenerationStatus.COMPLETED).exists():
        return "used"
    return "in_progress" if Lecture.objects.filter(user=user).exists() else "unused"


def admin_wallet_transactions(user: Any, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "id": str(tx.id),
            "kind": tx.kind,
            "amount_toman": int(tx.amount_toman),
            "paid_part": int(tx.paid_part),
            "gift_part": int(tx.gift_part),
            "balance_after": int(tx.balance_after),
            "source": str((tx.metadata or {}).get("source", "")),
            "created_at": tx.created_at.isoformat(),
        }
        for tx in WalletTransaction.objects.filter(user=user).order_by("-created_at")[:limit]
    ]


def admin_wallet_snapshot(user: Any) -> dict[str, Any]:
    """Read-only view of a user's wallet for the admin (never converts or creates anything)."""
    from django.db.models import Count, Sum

    balance = balance_summary(user)
    topups = WalletTransaction.objects.filter(user=user, kind=WalletTxKind.TOPUP).aggregate(n=Count("id"), total=Sum("amount_toman"))
    spent = WalletTransaction.objects.filter(
        user=user, kind__in=[WalletTxKind.NOTE_CHARGE, WalletTxKind.ADDON_CHARGE]
    ).aggregate(total=Sum("amount_toman"))
    gifts = {
        row["kind"]: int(row["total"] or 0)
        for row in WalletTransaction.objects.filter(user=user, kind__in=[WalletTxKind.GIFT, WalletTxKind.GIFT_EXPIRED])
        .values("kind")
        .annotate(total=Sum("amount_toman"))
    }
    spent_parts = WalletTransaction.objects.filter(
        user=user, kind__in=[WalletTxKind.NOTE_CHARGE, WalletTxKind.ADDON_CHARGE]
    ).aggregate(gift=Sum("gift_part"), paid=Sum("paid_part"))
    return {
        "active": bool(wallet_active_for(user)),
        **balance,
        # The lifetime picture, so a gift that is already spent is still visible.
        "gift_received_toman": gifts.get(WalletTxKind.GIFT, 0),
        "gift_expired_toman": -gifts.get(WalletTxKind.GIFT_EXPIRED, 0),
        "gift_spent_toman": -int(spent_parts["gift"] or 0),
        "paid_spent_toman": -int(spent_parts["paid"] or 0),
        "topup_count": int(topups["n"] or 0),
        "topup_total_toman": int(topups["total"] or 0),
        "spent_toman": -int(spent["total"] or 0),
        "first_note": first_note_state(user),
    }


def _first_note_still_free(user: Any) -> bool:
    from apps.subscriptions.models import UserSubscription
    from apps.uploads.services import SubscriptionValidationService

    try:
        subscription = (
            UserSubscription.objects.select_related("plan").filter(user=user, is_active=True).first()
        )
        if subscription is None:
            return False
        return SubscriptionValidationService.first_note_full_applies(
            subscription=subscription, remaining_minutes=int(subscription.available_minutes), user=user
        )
    except Exception:  # noqa: BLE001 - a display hint must never break the user payload
        logger.exception("first_note_free_failed", extra={"user_id": str(getattr(user, "id", ""))})
        return False


def wallet_summary(user: Any) -> dict[str, Any] | None:
    if not wallet_active_for(user):
        return None
    try:
        convert_legacy_free_minutes(user)  # their unused free minutes become gift the first time they look
    except Exception:
        logger.exception("legacy_free_minutes_failed", extra={"user_id": str(getattr(user, "id", ""))})
    balance = balance_summary(user)
    return {
        "active": True,
        # Still has the free first note: the UI shows "free" instead of a zero balance.
        "first_note_free": _first_note_still_free(user),
        **balance,
        "estimate_toman_per_minute": round(pricing.per_minute_estimate_toman(), 2),
        "estimate_toman_per_60_minutes": pricing.estimate_for_minutes(60),
        "min_topup_toman": int(settings.WALLET_MIN_TOPUP_TOMAN),
        "max_topup_toman": int(settings.WALLET_MAX_TOPUP_TOMAN),
        "offer": offer_state(user),
        "show_plan_label": bool(settings.WALLET_SHOW_PLAN_LABEL),
    }


def addon_block_response(user: Any, *, note_id: Any = None, feature: str = ""):
    """A 403 response for wallet users who cannot start an add-on, else None.

    Allowed when the user has a little balance, or when this is an add-on of their first (free) note:
    summary, flashcards and quiz are free there, chat for the first few messages.
    """
    from rest_framework import status
    from rest_framework.response import Response

    note = None
    if note_id is not None:
        from apps.notes.models import GeneratedNote

        note = GeneratedNote.objects.filter(id=note_id, user=user).first()
    if addon_allowed(user) or addon_free_on_first_note(user, note, feature):
        return None
    on_first_note = (
        note is not None and feature == "chatbot" and wallet_active_for(user) and note.lecture_id == first_note_lecture_id(user)
    )
    message = (
        "پیام‌های رایگان دستیار برای جزوهٔ اولت تموم شد؛ برای استفاده بیشتر باید حسابت رو شارژ کنی."
        if on_first_note
        else "موجودی حسابت کافی نیست؛ برای ادامه حساب را شارژ کن."
    )
    return Response(
        {
            "success": False,
            "error_code": "INSUFFICIENT_WALLET_BALANCE",
            "message": message,
            "details": {
                "wallet": True,
                "available_toman": available_toman(user),
                **({"free_chat_messages": free_chat_messages_limit()} if on_first_note else {}),
            },
        },
        status=status.HTTP_403_FORBIDDEN,
    )
