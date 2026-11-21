"""Prepaid wallet (toman). The ledger is the source of truth; balances are kept on the account row
and always changed together with a ledger row inside one transaction.

Spending order: gift lots (soonest expiry first), then the paid balance. The paid balance may go
slightly negative when a note turns out dearer than its hold; the user then tops up before the next
one (the admission check uses the available balance, which counts that debt).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models

from apps.common.models import BaseModel


class WalletAccount(BaseModel):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet")
    paid_toman = models.BigIntegerField(default=0)
    gift_toman = models.BigIntegerField(default=0)
    held_toman = models.BigIntegerField(default=0)

    @property
    def available_toman(self) -> int:
        return int(self.paid_toman + self.gift_toman - self.held_toman)

    def __str__(self) -> str:
        return f"wallet {self.user_id}: {self.paid_toman}+{self.gift_toman}-{self.held_toman}"


class WalletGiftLot(BaseModel):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet_gift_lots")
    amount_toman = models.BigIntegerField()
    remaining_toman = models.BigIntegerField()
    expires_at = models.DateTimeField(db_index=True)
    source = models.CharField(max_length=32, default="first_topup")

    class Meta:
        ordering = ("expires_at",)
        indexes = [models.Index(fields=["user", "remaining_toman", "expires_at"], name="wallet_lot_spend_idx")]


class WalletTxKind(models.TextChoices):
    TOPUP = "TOPUP", "Top-up"
    GIFT = "GIFT", "Gift credit"
    GIFT_EXPIRED = "GIFT_EXPIRED", "Gift expired"
    NOTE_CHARGE = "NOTE_CHARGE", "Note charge"
    ADDON_CHARGE = "ADDON_CHARGE", "Add-on charge"
    COST_ADJUSTMENT = "COST_ADJUSTMENT", "Cost adjustment"
    REFUND = "REFUND", "Refund"
    ADMIN = "ADMIN", "Admin adjustment"


class WalletTransaction(BaseModel):
    """Money that really moved. Holds are separate (`WalletHold`) and never appear here."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet_transactions")
    kind = models.CharField(max_length=24, choices=WalletTxKind.choices, db_index=True)
    amount_toman = models.BigIntegerField()  # + credit, - debit
    paid_part = models.BigIntegerField(default=0)
    gift_part = models.BigIntegerField(default=0)
    idempotency_key = models.CharField(max_length=120, unique=True)
    ref_type = models.CharField(max_length=32, blank=True)
    ref_id = models.CharField(max_length=64, blank=True, db_index=True)
    balance_after = models.BigIntegerField(default=0)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["user", "created_at"], name="wallet_tx_user_time_idx")]


class WalletHoldStatus(models.TextChoices):
    HELD = "HELD", "Held"
    SETTLED = "SETTLED", "Settled"
    RELEASED = "RELEASED", "Released"


class WalletHold(BaseModel):
    """Money set aside for one lecture while it is being processed."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet_holds")
    lecture = models.OneToOneField("lectures.Lecture", on_delete=models.CASCADE, related_name="wallet_hold")
    amount_toman = models.BigIntegerField()
    estimate_toman = models.BigIntegerField(default=0)
    status = models.CharField(max_length=12, choices=WalletHoldStatus.choices, default=WalletHoldStatus.HELD, db_index=True)
    charged_toman = models.BigIntegerField(default=0)
    cost_breakdown = models.JSONField(default=dict, blank=True)


class OfferState(models.TextChoices):
    STARTED = "STARTED", "Started"
    SKIPPED = "SKIPPED", "Skipped"
    CLAIMED = "CLAIMED", "Claimed"
    EXPIRED = "EXPIRED", "Expired"


class TopupOffer(BaseModel):
    """The first-top-up gift window: created once per user, never restarted or extended."""

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet_offer")
    started_at = models.DateTimeField()
    expires_at = models.DateTimeField(db_index=True)
    state = models.CharField(max_length=12, choices=OfferState.choices, default=OfferState.STARTED, db_index=True)
    skipped_at = models.DateTimeField(null=True, blank=True)
    claimed_payment_id = models.CharField(max_length=64, blank=True)
