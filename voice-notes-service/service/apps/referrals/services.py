from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import timedelta
from pathlib import PurePosixPath

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.utils import timezone

from apps.accounts.models import User
from apps.lectures.models import Lecture, LectureProcessingState, LectureStatus
from apps.notes.models import GeneratedNote, NoteGenerationStatus, NoteImage
from apps.notes.services import GeneratedNoteService
from apps.notes.themes import normalize_note_theme
from apps.referrals.models import (
    NoteShareBundle,
    NoteShareBundleItem,
    NoteShareLink,
    ReferralStatus,
    ShareEvent,
    ShareEventKind,
    ShareReferral,
    SharedNoteCopy,
)
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from infrastructure.storage.factory import get_storage_service
from services.subscriptions.lifecycle import SubscriptionLifecycleService

logger = logging.getLogger("tesanote")

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
CODE_PATTERN = re.compile(r"^[A-Z0-9]{6,12}$")
_PAGE_MARKER = '<div class="a4-page">'
_SCRIPT_PATTERN = re.compile(r"<script\b.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

_PREVIEW_STYLE = """
<style id="tesanote-share-preview">
@media screen {
  html, body { width: 100%; min-width: 0; }
  body { display: block !important; min-height: 100% !important; padding: 0 !important; overflow-x: hidden; }
  .template-wrapper { width: 100% !important; max-width: none !important; gap: 18px !important; }
  .a4-page { box-sizing: border-box; width: 100% !important; max-width: none !important; margin: 0 !important; }
  .page-footer__page-number { display: none !important; }
}
</style>
"""


def _visitor_uuid(raw) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


def ensure_referral_code(*, user: User) -> str:
    if user.referral_code:
        return user.referral_code
    for _ in range(8):
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
        try:
            with transaction.atomic():
                updated = User.objects.filter(pk=user.pk, referral_code__isnull=True).update(referral_code=code)
        except IntegrityError:
            continue
        if updated:
            user.referral_code = code
            return code
        user.refresh_from_db(fields=["referral_code"])
        if user.referral_code:
            return user.referral_code
    raise RuntimeError("Could not allocate a referral code.")


@transaction.atomic
def get_or_create_share_link(*, note: GeneratedNote, owner: User) -> NoteShareLink:
    link = NoteShareLink.objects.filter(note=note, revoked_at__isnull=True).first()
    if link is not None:
        return link
    link = NoteShareLink.objects.create(note=note, owner=owner, token=secrets.token_urlsafe(32))
    ShareEvent.objects.create(link=link, kind=ShareEventKind.CREATED, user=owner)
    return link


def revoke_share_link(*, link: NoteShareLink) -> bool:
    return bool(
        NoteShareLink.objects.filter(pk=link.pk, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), updated_at=timezone.now()
        )
    )


def active_link_for_token(token: str) -> NoteShareLink | None:
    if not TOKEN_PATTERN.fullmatch(token or ""):
        return None
    link = (
        NoteShareLink.objects.select_related("note", "note__lecture")
        .filter(token=token, revoked_at__isnull=True)
        .first()
    )
    if link is None or link.note.status != NoteGenerationStatus.COMPLETED:
        return None
    return link


def build_preview_html(*, note: GeneratedNote) -> tuple[str, int, int]:
    """Return (html, previewed_pages, total_pages): only the first pages, scripts removed."""
    body = _SCRIPT_PATTERN.sub("", note.body_html or "")
    positions = [match.start() for match in re.finditer(re.escape(_PAGE_MARKER), body)]
    total = len(positions)
    limit = max(1, int(settings.SHARE_PREVIEW_PAGES))
    if total > limit:
        body = body[: positions[limit]]
    metadata = note.generation_metadata or {}
    full_html = GeneratedNoteService.render_full_html_from_body(
        body_content=body,
        theme=normalize_note_theme(note.theme or note.lecture.processing_metadata.get("note_theme")),
        language_code=str(metadata.get("output_language") or note.lecture.output_language or "fa"),
        direction=str(metadata.get("output_direction") or "rtl"),
    )
    full_html = GeneratedNoteService._inline_note_images_for_editor(note=note, html=full_html)
    return full_html.replace("</head>", f"{_PREVIEW_STYLE}</head>", 1), min(limit, total), total


def record_view(*, link: NoteShareLink, visitor_id) -> None:
    visitor = _visitor_uuid(visitor_id)
    if visitor is None:
        return
    if ShareEvent.objects.filter(link=link, kind=ShareEventKind.VIEWED, visitor_id=visitor).exists():
        return
    ShareEvent.objects.create(link=link, kind=ShareEventKind.VIEWED, visitor_id=visitor)


def _minutes_granted_this_month(*, referrer: User) -> int:
    since = timezone.now() - timedelta(days=30)
    signup = ShareReferral.objects.filter(referrer=referrer, created_at__gte=since).aggregate(
        total=Sum("minutes_awarded")
    )["total"]
    purchase = ShareReferral.objects.filter(referrer=referrer, purchase_rewarded_at__gte=since).aggregate(
        total=Sum("purchase_minutes_awarded")
    )["total"]
    return int(signup or 0) + int(purchase or 0)


def attach_referral(*, user: User, created: bool, share_token=None, referral_code=None, visitor_id=None) -> ShareReferral | None:
    """Attribute a brand-new account to a share link or referral code and pay the referrer.

    Only newly created accounts qualify; phone numbers are unique, so one
    person cannot register twice for the reward. The monthly cap and the
    one-row-per-referred-user constraint make this safe to retry.
    """
    if not created:
        return None
    link = active_link_for_token(str(share_token or "").strip()) if share_token else None
    referrer: User | None = link.owner if link is not None else None
    source = "share_link"
    if referrer is None and referral_code:
        code = str(referral_code).strip().upper()
        if CODE_PATTERN.fullmatch(code):
            referrer = User.objects.filter(referral_code=code, is_active=True).first()
            source = "referral_code"
    if referrer is None or referrer.pk == user.pk:
        return None

    with transaction.atomic():
        if ShareReferral.objects.filter(referred_user=user).exists():
            return None
        reward = int(settings.REFERRAL_REWARD_MINUTES)
        remaining_cap = max(0, int(settings.REFERRAL_MONTHLY_CAP_MINUTES) - _minutes_granted_this_month(referrer=referrer))
        awarded = min(reward, remaining_cap)
        # The friend's welcome bonus does not depend on the referrer's monthly cap.
        friend_bonus = max(0, int(settings.REFERRAL_FRIEND_BONUS_MINUTES))
        try:
            referral = ShareReferral.objects.create(
                referred_user=user,
                referrer=referrer,
                link=link,
                source=source,
                minutes_awarded=awarded,
                friend_minutes_awarded=friend_bonus,
                status=ReferralStatus.CAPPED if reward > 0 and awarded == 0 else ReferralStatus.GRANTED,
            )
        except IntegrityError:
            return None
        User.objects.filter(pk=user.pk, referred_by__isnull=True).update(referred_by=referrer)
        if link is not None:
            ShareEvent.objects.create(
                link=link, kind=ShareEventKind.SIGNED_UP, user=user, visitor_id=_visitor_uuid(visitor_id)
            )
        if awarded > 0:
            SubscriptionLifecycleService.add_referral_minutes(user=referrer, minutes=awarded)
        if friend_bonus > 0:
            SubscriptionLifecycleService.add_referral_minutes(user=user, minutes=friend_bonus)
    return referral


def reward_referrer_for_purchase(*, user: User) -> int:
    """Pay the referrer once when the person they invited buys a plan. Returns the minutes paid.

    Safe to call on every successful plan payment: the row is locked and stamped, so only the
    first purchase of a referred user ever pays, and it never raises into the payment flow.
    """
    try:
        with transaction.atomic():
            referral = ShareReferral.objects.select_for_update().filter(referred_user=user).first()
            if referral is None or referral.purchase_rewarded_at is not None:
                return 0
            reward = max(0, int(settings.REFERRAL_PURCHASE_REWARD_MINUTES))
            remaining_cap = max(
                0, int(settings.REFERRAL_MONTHLY_CAP_MINUTES) - _minutes_granted_this_month(referrer=referral.referrer)
            )
            awarded = min(reward, remaining_cap)
            referral.purchase_minutes_awarded = awarded
            referral.purchase_rewarded_at = timezone.now()
            referral.save(update_fields=["purchase_minutes_awarded", "purchase_rewarded_at", "updated_at"])
            if awarded > 0:
                SubscriptionLifecycleService.add_referral_minutes(user=referral.referrer, minutes=awarded)
            logger.info(
                "referral_purchase_reward",
                extra={"referrer_id": str(referral.referrer_id), "referred_id": str(user.id), "minutes": awarded},
            )
            return awarded
    except Exception:  # noqa: BLE001 - a gift must never break a payment
        logger.exception("referral_purchase_reward_failed", extra={"user_id": str(user.id)})
        return 0


def record_activation(*, user: User) -> None:
    """Mark the share-link signup as activated once the invitee processes a lecture."""
    referral = ShareReferral.objects.select_related("link").filter(referred_user=user, link__isnull=False).first()
    if referral is None:
        return
    ShareEvent.objects.get_or_create(link=referral.link, user=user, kind=ShareEventKind.ACTIVATED)


_NOTE_IMAGE_URL_PATTERN = re.compile(
    r"(/api/v1/notes/)([0-9a-fA-F-]{36})(/images/)([0-9a-fA-F-]{36})(/)"
)
# Only these keys are carried over; prompts, source text and provider details stay with the owner.
_COPIED_GENERATION_METADATA_KEYS = ("output_language", "output_direction", "source_mode")


def _copy_note_images(*, source: GeneratedNote, copy: GeneratedNote, user: User) -> list[str]:
    """Duplicate the source note's embedded photos for the copy and repoint its body at them.

    Returns the storage paths written so the caller can remove them if the
    surrounding transaction fails.
    """
    body = copy.body_html or ""
    referenced = {match.group(4).lower() for match in _NOTE_IMAGE_URL_PATTERN.finditer(body)}
    if not referenced:
        return []
    storage = get_storage_service()
    id_map: dict[str, str] = {}
    written: list[str] = []
    for image in NoteImage.objects.filter(note=source):
        old_id = str(image.id).lower()
        if old_id not in referenced or not storage.exists(image.storage_path):
            continue
        new_id = uuid.uuid4()
        extension = PurePosixPath(image.storage_path).suffix or ".jpg"
        with storage.open(image.storage_path, "rb") as stream:
            content = stream.read()
        stored = storage.save_content(
            relative_path=f"notes/{copy.id}/images/{new_id}{extension}",
            content=content,
            content_type=image.content_type,
        )
        written.append(stored.path)
        StorageArtifactLifecycleService.register_persistent(
            user_id=str(user.id),
            lecture_id=str(copy.lecture_id),
            storage_path=stored.path,
            size_bytes=stored.size,
            artifact_type="note_image",
        )
        NoteImage.objects.create(
            id=new_id,
            user=user,
            note=copy,
            storage_path=stored.path,
            content_type=image.content_type,
            size_bytes=stored.size,
            width=image.width,
            height=image.height,
        )
        id_map[old_id] = str(new_id)

    def _repoint(match: re.Match[str]) -> str:
        new_image_id = id_map.get(match.group(4).lower())
        if new_image_id is None:
            return match.group(0)
        return f"{match.group(1)}{copy.id}{match.group(3)}{new_image_id}{match.group(5)}"

    copy.body_html = _NOTE_IMAGE_URL_PATTERN.sub(_repoint, body)
    copy.save(update_fields=["body_html", "updated_at"])
    return written


def claim_shared_note(*, link: NoteShareLink, user: User) -> tuple[GeneratedNote, bool]:
    """Give `user` their own editable copy of a shared note; idempotent per (note, user).

    Returns (note, created). The owner just gets their original note back.
    """
    source = link.note
    if source.user_id == user.pk:
        return source, False

    written: list[str] = []
    try:
        with transaction.atomic():
            User.objects.select_for_update().get(pk=user.pk)
            existing = (
                SharedNoteCopy.objects.select_related("copied_note")
                .filter(source_note=source, user=user)
                .first()
            )
            if existing is not None:
                return existing.copied_note, False

            now = timezone.now()
            source_lecture = source.lecture
            metadata = source.generation_metadata or {}
            lecture = Lecture.objects.create(
                user=user,
                title=source_lecture.title,
                course_name=source_lecture.course_name,
                professor_name=source_lecture.professor_name,
                session_number=source_lecture.session_number,
                input_language=source_lecture.input_language,
                output_language=source_lecture.output_language,
                note_detail_mode=source_lecture.note_detail_mode,
                status=LectureStatus.COMPLETED,
                processing_state=LectureProcessingState.COMPLETED,
                processing_progress=100,
                processing_started_at=now,
                processing_completed_at=now,
                processing_metadata={
                    "note_theme": normalize_note_theme(source.theme),
                    "shared_copy": True,
                    "source_note_id": str(source.id),
                },
            )
            copy = GeneratedNote.objects.create(
                user=user,
                lecture=lecture,
                variant=source.variant,
                model_name=source.model_name,
                status=NoteGenerationStatus.COMPLETED,
                progress=100,
                title=source.title or source_lecture.title,
                theme=source.theme,
                summary_text=source.summary_text,
                semantic_payload=source.semantic_payload or {},
                body_html=source.body_html,
                generation_metadata={
                    **{key: metadata[key] for key in _COPIED_GENERATION_METADATA_KEYS if key in metadata},
                    "shared_from_note": str(source.id),
                },
            )
            SharedNoteCopy.objects.create(source_note=source, link=link, user=user, copied_note=copy)
            written = _copy_note_images(source=source, copy=copy, user=user)
            ShareEvent.objects.get_or_create(link=link, user=user, kind=ShareEventKind.CLAIMED)
            return copy, True
    except Exception:
        storage = get_storage_service()
        for path in written:
            try:
                storage.delete(path)
            except Exception:
                logger.exception("shared_note_copy_cleanup_failed", extra={"storage_path": path})
        raise


MAX_BUNDLE_NOTES = 20


@transaction.atomic
def get_or_create_share_bundle(*, notes: list[GeneratedNote], owner: User) -> NoteShareBundle:
    """One bundle link for exactly this set of notes; asking again for the same set returns the same link."""
    ordered: list[GeneratedNote] = []
    seen: set[uuid.UUID] = set()
    for note in notes:
        if note.pk not in seen:
            seen.add(note.pk)
            ordered.append(note)
    wanted_ids = {note.pk for note in ordered}
    for bundle in NoteShareBundle.objects.filter(owner=owner, revoked_at__isnull=True).prefetch_related("items__link"):
        if {item.link.note_id for item in bundle.items.all()} == wanted_ids:
            for note in ordered:  # a note whose own link was revoked gets a fresh one
                link = get_or_create_share_link(note=note, owner=owner)
                NoteShareBundleItem.objects.get_or_create(bundle=bundle, link=link)
            return bundle
    bundle = NoteShareBundle.objects.create(owner=owner, token=secrets.token_urlsafe(32))
    for position, note in enumerate(ordered):
        link = get_or_create_share_link(note=note, owner=owner)
        NoteShareBundleItem.objects.create(bundle=bundle, link=link, position=position)
    return bundle


def active_bundle_links(token: str) -> tuple[NoteShareBundle, list[NoteShareLink]] | None:
    """The bundle and the links that are still shareable (not revoked, note finished); None if none are."""
    if not TOKEN_PATTERN.fullmatch(token or ""):
        return None
    bundle = NoteShareBundle.objects.filter(token=token, revoked_at__isnull=True).first()
    if bundle is None:
        return None
    links = [
        item.link
        for item in bundle.items.select_related("link", "link__note", "link__note__lecture").order_by("position")
        if item.link.revoked_at is None and item.link.note.status == NoteGenerationStatus.COMPLETED
    ]
    return (bundle, links) if links else None


def claim_shared_bundle(*, links: list[NoteShareLink], user: User) -> list[tuple[GeneratedNote, bool]]:
    """Give `user` their own copy of every note in the bundle (idempotent per note)."""
    return [claim_shared_note(link=link, user=user) for link in links]
