from __future__ import annotations

from django.db import models
from django.db.models import Q

from apps.common.models import BaseModel


class NoteShareLink(BaseModel):
    """Private, revocable read-only preview link for one note."""

    note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="share_links")
    owner = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_share_links")
    token = models.CharField(max_length=64, unique=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("note",),
                condition=Q(revoked_at__isnull=True),
                name="unique_active_share_link_per_note",
            ),
        ]

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def __str__(self) -> str:
        return f"Share link for note {self.note_id}"


class ShareEventKind(models.TextChoices):
    CREATED = "created", "Link created"
    VIEWED = "viewed", "Viewed"
    SIGNED_UP = "signed_up", "Signed up"
    ACTIVATED = "activated", "Activated"
    CLAIMED = "claimed", "Added to library"


class ShareEvent(BaseModel):
    link = models.ForeignKey(NoteShareLink, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=16, choices=ShareEventKind.choices, db_index=True)
    user = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    visitor_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["link", "kind"])]
        constraints = [
            models.UniqueConstraint(
                fields=("link", "user", "kind"),
                condition=Q(user__isnull=False),
                name="unique_share_event_per_link_user_kind",
            ),
        ]


class ReferralStatus(models.TextChoices):
    GRANTED = "granted", "Reward granted"
    CAPPED = "capped", "Monthly cap reached"


class ShareReferral(BaseModel):
    """One row per referred account; the OneToOne makes the reward idempotent."""

    referred_user = models.OneToOneField("accounts.User", on_delete=models.CASCADE, related_name="referral_record")
    referrer = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="referral_rewards")
    link = models.ForeignKey(NoteShareLink, on_delete=models.SET_NULL, null=True, blank=True, related_name="referrals")
    source = models.CharField(max_length=16, default="share_link")
    minutes_awarded = models.PositiveIntegerField(default=0)
    # Bonus minutes the invited friend received for joining through this referral.
    friend_minutes_awarded = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, choices=ReferralStatus.choices, default=ReferralStatus.GRANTED)
    # Paid to the referrer once, when the invited friend buys their first plan.
    purchase_minutes_awarded = models.PositiveIntegerField(default=0)
    purchase_rewarded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["referrer", "created_at"])]


class SharedNoteCopy(BaseModel):
    """A shared note copied into a recipient's own library; one copy per (note, recipient)."""

    source_note = models.ForeignKey("notes.GeneratedNote", on_delete=models.CASCADE, related_name="shared_copies")
    link = models.ForeignKey(NoteShareLink, on_delete=models.SET_NULL, null=True, blank=True, related_name="copies")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="shared_note_copies")
    copied_note = models.OneToOneField("notes.GeneratedNote", on_delete=models.CASCADE, related_name="shared_copy_record")

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(fields=("source_note", "user"), name="unique_shared_copy_per_note_user"),
        ]


class NoteShareBundle(BaseModel):
    """One private link that opens several of an owner's notes together.

    The notes keep their own NoteShareLink, so views, sign-ups and referral rewards are still counted per
    note; a bundle only groups them. Revoking a note's own link drops it from the bundle.
    """

    owner = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="note_share_bundles")
    token = models.CharField(max_length=64, unique=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    links = models.ManyToManyField(NoteShareLink, through="NoteShareBundleItem", related_name="bundles")

    class Meta:
        ordering = ("-created_at",)

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class NoteShareBundleItem(BaseModel):
    bundle = models.ForeignKey(NoteShareBundle, on_delete=models.CASCADE, related_name="items")
    link = models.ForeignKey(NoteShareLink, on_delete=models.CASCADE, related_name="bundle_items")
    position = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ("position",)
        constraints = [
            models.UniqueConstraint(fields=("bundle", "link"), name="unique_link_per_share_bundle"),
        ]
