from __future__ import annotations

from typing import Final


NOTE_THEMES: Final[tuple[str, ...]] = (
    "blue",
    "purple",
    "green",
    "orange",
    "pink",
    "gray",
)

DEFAULT_NOTE_THEME: Final[str] = "green"


def normalize_note_theme(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in NOTE_THEMES:
        return candidate
    return DEFAULT_NOTE_THEME

