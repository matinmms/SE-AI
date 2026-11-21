"""Names that must survive when a section is condensed to its length budget.

Condensing keeps the explanation and drops detail, and the detail it drops first is a list of named
things (drugs, algorithms, people, products). A name is what a student looks up and is tested on, so
the names of a section are collected before it is condensed and any that did not survive are put
back as one short list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

MAX_RESTORED_TERMS = 60

_BOLD = re.compile(r"\*\*(.{2,60}?)\*\*")
_KEY = re.compile(r"\[\[key:(.{2,60}?)\]\]")
_LATIN_WORD = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z\-]{4,}(?![A-Za-z0-9_])")
_ACRONYM = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z]{1,}[0-9]*[A-Z0-9]+[a-z]*(?![A-Za-z0-9_])")
_GENERIC_ENGLISH = frozenset(
    {
        "about", "above", "after", "again", "also", "because", "before", "being", "between", "could",
        "during", "every", "first", "from", "have", "into", "made", "more", "most", "other", "should",
        "since", "some", "such", "than", "that", "their", "them", "then", "there", "these", "they",
        "this", "those", "through", "under", "until", "very", "what", "when", "where", "which", "while",
        "with", "would", "tesanote", "note", "notes", "lecture", "figure", "example", "table", "slide",
    }
)


def _clean(term: str) -> str:
    return re.sub(r"\s+", " ", term).strip(" .,:;،؛()[]\"'")


def protected_terms(texts: Iterable[str]) -> list[str]:
    """Bold and key-marked terms, Latin names and acronyms, in order of first appearance."""
    found: dict[str, str] = {}
    for text in texts:
        value = str(text or "")
        candidates: list[tuple[int, str]] = []
        for pattern in (_BOLD, _KEY):
            candidates.extend((m.start(), m.group(1)) for m in pattern.finditer(value))
        candidates.extend((m.start(), m.group(0)) for m in _LATIN_WORD.finditer(value))
        candidates.extend((m.start(), m.group(0)) for m in _ACRONYM.finditer(value))
        for _, raw in sorted(candidates):
            term = _clean(raw)
            key = term.casefold()
            if len(term) < 3 or key in _GENERIC_ENGLISH or key in found:
                continue
            found[key] = term
    return list(found.values())


def lost_terms(before: Iterable[str], after: Iterable[str]) -> list[str]:
    """Terms present in ``before`` that no longer appear anywhere in ``after``."""
    remaining = "\n".join(str(text or "") for text in after).casefold()
    lost = [term for term in protected_terms(before) if term.casefold() not in remaining]
    return lost[:MAX_RESTORED_TERMS]
