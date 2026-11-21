from __future__ import annotations

import re


class NoteTemplateValidationError(ValueError):
    """Raised when generated note content violates the body-only contract."""


FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("doctype", re.compile(r"<!DOCTYPE", re.IGNORECASE)),
    ("html tag", re.compile(r"</?html\b", re.IGNORECASE)),
    ("head tag", re.compile(r"</?head\b", re.IGNORECASE)),
    ("body tag", re.compile(r"</?body\b", re.IGNORECASE)),
    ("style tag", re.compile(r"</?style\b", re.IGNORECASE)),
    ("script tag", re.compile(r"</?script\b", re.IGNORECASE)),
    ("meta tag", re.compile(r"<meta\b", re.IGNORECASE)),
    ("link tag", re.compile(r"<link\b", re.IGNORECASE)),
    ("title tag", re.compile(r"</?title\b", re.IGNORECASE)),
    ("MathJax config", re.compile(r"MathJax", re.IGNORECASE)),
    ("template placeholder", re.compile(r"\{\{\s*CONTENT\s*\}\}", re.IGNORECASE)),
)


def validate_body_fragment(fragment: str) -> str:
    normalized = (fragment or "").strip()
    if not normalized:
        raise NoteTemplateValidationError("Generated note body is empty.")

    for label, pattern in FORBIDDEN_PATTERNS:
        if pattern.search(normalized):
            raise NoteTemplateValidationError(f"Generated note body contains forbidden {label}.")
    return normalized

