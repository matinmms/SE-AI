"""The three ways a note can be written, chosen by the student.

- ``explained`` (توضیح تسانت): the v4.2 teaching note, TesaNote explains and adds marked examples.
- ``lecturer`` (دقیقاً حرف استاد): the v2.2 note, the lecturer's own words and order, nothing added.
- ``transcript`` (متن کامل کلاس): the whole class, only misheard words corrected and paragraphs made.

The lecture's chosen style is stored in ``processing_metadata["note_style"]`` when processing starts.
The other two can be made later from the note page; they are stored as extra notes of the same
lecture under ``<model>-alt-<style>`` variants, which never decide the lecture's status.
"""
from __future__ import annotations

from typing import Any

from django.conf import settings

STYLE_EXPLAINED = "explained"
STYLE_LECTURER = "lecturer"
STYLE_TRANSCRIPT = "transcript"
NOTE_STYLES = (STYLE_EXPLAINED, STYLE_LECTURER, STYLE_TRANSCRIPT)
DEFAULT_STYLE = STYLE_EXPLAINED

ALT_VARIANT_MARKER = "-alt-"

STYLE_TITLE_LABELS = {
    STYLE_LECTURER: " — حرف استاد",
    STYLE_TRANSCRIPT: " — متن کامل کلاس",
    STYLE_EXPLAINED: " — توضیح تسانت",
}


def normalize_style(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in NOTE_STYLES else ""


def style_choice_enabled_for(user: Any) -> bool:
    """Who may pick a style. Staff only until v2.2 and the transcript pass review (NOTE_STYLE_CHOICE_STAFF_ONLY)."""
    if user is None or not getattr(settings, "NOTE_STYLE_CHOICE_ENABLED", False):
        return False
    if getattr(settings, "NOTE_STYLE_CHOICE_STAFF_ONLY", True):
        return bool(getattr(user, "is_staff", False))
    return True


def lecture_style(lecture: Any) -> str:
    """The style the lecture's main note is written in ("explained" when none was chosen)."""
    if not style_choice_enabled_for(getattr(lecture, "user", None)):
        return DEFAULT_STYLE
    metadata = getattr(lecture, "processing_metadata", None) or {}
    return normalize_style(metadata.get("note_style")) or DEFAULT_STYLE


def alt_variant(base_variant: str, style: str) -> str:
    return f"{base_variant}{ALT_VARIANT_MARKER}{style}"


def is_alt_variant(variant: str) -> bool:
    return ALT_VARIANT_MARKER in str(variant or "")


def style_of_variant(variant: str) -> str:
    text = str(variant or "")
    if ALT_VARIANT_MARKER in text:
        return normalize_style(text.rsplit(ALT_VARIANT_MARKER, 1)[1])
    return ""


def style_of_note(note: Any) -> str:
    """The style a stored note was written in."""
    from_variant = style_of_variant(getattr(note, "variant", ""))
    if from_variant:
        return from_variant
    if str(getattr(note, "variant", "") or "").endswith("-compare-v2"):
        return STYLE_LECTURER
    metadata = getattr(note, "generation_metadata", None) or {}
    return normalize_style(metadata.get("note_style")) or lecture_style(getattr(note, "lecture", None))


def version_price_toman(*, style: str, minutes: int) -> int:
    """What making another version costs a wallet user, from the class length (whole toman)."""
    from apps.wallet import pricing

    full = pricing.estimate_for_minutes(minutes)
    if style == STYLE_TRANSCRIPT:
        percent = int(getattr(settings, "NOTE_STYLE_TRANSCRIPT_PRICE_PERCENT", 50))
        return pricing.round_up(full * percent / 100, 500)
    return full
