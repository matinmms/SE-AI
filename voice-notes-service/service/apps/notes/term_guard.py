"""Guards that compare a finished note with the transcript (2026-11-18 fix plan, phase B).

The QA reviews found Latin-script words printed as terms whose only source was a stretch of speech the
recognizer broke, figures whose unit differed from the lecturer's, and exam boxes with no exam remark
behind them. The writer prompt already forbids these, but it does not hold, so these checks run
in code after the writer. All are free (no model call) and conservative:

* `garbled_latin_terms` / `apply_term_guard`: a Latin term is *garbled* only when every place it occurs in
  the lecture is a stretch `reliability.garble_level` marks as broken, and it is not in the slides, the course
  name or title. A term found in a clear stretch, or found nowhere (a correct addition of the teaching note),
  is never touched. The default mode is `report`: nothing is removed, the terms go to the audit.
* `unit_changes`: the same number with a different unit word in the note and in the lecture (hours vs
  minutes, micrometre vs nanometre). Report only: both cases found by the reviews were probably
  corrections of a slip, so the note is not changed silently.
* `demote_unbacked_exam_statements`: an exam statement with no exam, importance, assignment or review cue of the
  lecturer behind it is shown as a plain note, not as an exam point.
"""

from __future__ import annotations

import re
from typing import Any

from apps.notes.foreign_terms import latin_terms
from apps.notes.lecturer_cues import (
    KIND_ASSIGNMENT,
    KIND_DEADLINE,
    _classify,
    find_cues,
)
from apps.notes.reliability import garble_level
from services.note_review.analysis import tokenize

TERM_GUARD_MODES = ("off", "report", "enforce")
_TEXT_FIELDS = ("body", "note", "example", "key_idea")


def term_guard_mode(raw: Any) -> str:
    mode = str(raw or "report").strip().lower()
    return mode if mode in TERM_GUARD_MODES else "report"


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).casefold()


# --------------------------------------------------------------------------------------
# Latin terms that come only from broken speech
# --------------------------------------------------------------------------------------


def garbled_latin_terms(note_text: str, chunk_texts: list[str], allowed_text: str = "") -> list[str]:
    """Latin-script terms of the note whose every occurrence in the lecture is in a broken stretch."""
    clear = _fold(" ".join(text for text in chunk_texts if not garble_level(text)))
    damaged = [_fold(text) for text in chunk_texts if garble_level(text)]
    allowed = _fold(allowed_text)
    found: list[str] = []
    for term in latin_terms(note_text, limit=120):
        key = _fold(term)
        if key in clear or key in allowed:
            continue
        if any(key in text for text in damaged):
            found.append(term)
    return found


def _unit_texts(unit: dict[str, Any]) -> list[str]:
    parts = [str(unit.get(key, "") or "") for key in _TEXT_FIELDS if not (key == "example" and unit.get("example_ai"))]
    parts.extend(str(item or "") for item in unit.get("list_items", []) or [])
    return [part for part in parts if part.strip()]


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟۔])\s+")


def _remove_term(text: str, term: str) -> str:
    """Drop the term from a text: its parenthesis, or the sentences that carry it (never the whole text)."""
    source = str(text or "")
    parenthetical = re.compile(r"\s*[(（]\s*" + re.escape(term) + r"\s*[)）]", re.IGNORECASE)
    without = parenthetical.sub("", source)
    if without != source:
        return without
    paragraphs = re.split(r"(\n\s*\n)", source)
    key = _fold(term)
    rebuilt: list[str] = []
    changed = False
    for paragraph in paragraphs:
        if not paragraph.strip():
            rebuilt.append(paragraph)
            continue
        kept = [part for part in _SENTENCE_SPLIT.split(paragraph) if key not in _fold(part)]
        if len(kept) != len(_SENTENCE_SPLIT.split(paragraph)):
            changed = True
        rebuilt.append(" ".join(part.strip() for part in kept if part.strip()))
    result = "".join(rebuilt).strip()
    return result if changed and result else source


def apply_term_guard(
    units: list[dict[str, Any]],
    *,
    chunk_texts: list[str],
    allowed_text: str = "",
    mode: str = "report",
    transcript_text: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The units, with garbled Latin terms removed in `enforce` mode, and a report of what was found.

    The report also lists figures whose unit differs from the lecture's (`unit_changes`, never changed).
    """
    mode = term_guard_mode(mode)
    if mode == "off" or not units or not chunk_texts:
        return units, {"mode": mode, "terms": [], "changed_units": 0}
    note_text = "\n".join(text for unit in units for text in _unit_texts(unit))
    terms = garbled_latin_terms(note_text, chunk_texts, allowed_text)
    report: dict[str, Any] = {"mode": mode, "terms": terms[:40], "changed_units": 0}
    if transcript_text:
        report["unit_changes"] = unit_changes(note_text, transcript_text)[:20]
    if mode != "enforce" or not terms:
        return units, report
    result: list[dict[str, Any]] = []
    for unit in units:
        updated = dict(unit)
        changed = False
        for key in _TEXT_FIELDS:
            if key == "example" and unit.get("example_ai"):
                continue
            value = updated.get(key)
            if not isinstance(value, str):
                continue
            for term in terms:
                if _fold(term) in _fold(value):
                    new_value = _remove_term(value, term)
                    if new_value != value:
                        value, changed = new_value, True
            updated[key] = value
        items = updated.get("list_items")
        if isinstance(items, list):
            kept_items = [item for item in items if not any(_fold(term) in _fold(str(item or "")) for term in terms)]
            if len(kept_items) != len(items) and kept_items:
                updated["list_items"], changed = kept_items, True
        if changed:
            report["changed_units"] += 1
        result.append(updated)
    return result, report


# --------------------------------------------------------------------------------------
# A figure whose unit changed
# --------------------------------------------------------------------------------------

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_UNIT_WORDS = {
    "nanometre": r"نانو[‌ ]?متر|\bnm\b",
    "micrometre": r"میکرون|میکرو[‌ ]?متر|μm|\bum\b",
    "millimetre": r"میلی[‌ ]?متر",
    "second": r"ثانیه",
    "minute": r"دقیقه",
    "hour": r"ساعت",
    "percent": r"درصد|%",
    "degree": r"درجه|°",
}
_NUMBER_UNIT = re.compile(
    r"(?<![\w.])(\d+(?:[.,٫]\d+)?)\s*(" + "|".join(f"(?:{pattern})" for pattern in _UNIT_WORDS.values()) + ")"
)


def _unit_of(word: str) -> str:
    for name, pattern in _UNIT_WORDS.items():
        if re.fullmatch(pattern, word.strip(), re.IGNORECASE) or re.match(pattern, word.strip(), re.IGNORECASE):
            return name
    return ""


def _figures(text: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for match in _NUMBER_UNIT.finditer(str(text or "").translate(_DIGITS)):
        number = match.group(1).replace("٫", ".").replace(",", ".")
        unit = _unit_of(match.group(2))
        if unit:
            found.setdefault(number, set()).add(unit)
    return found


def unit_changes(note_text: str, transcript_text: str) -> list[dict[str, Any]]:
    """Numbers that appear with one unit in the note and a different one in the lecture."""
    in_note = _figures(note_text)
    in_lecture = _figures(transcript_text)
    changes: list[dict[str, Any]] = []
    for number, units in sorted(in_note.items()):
        lecture_units = in_lecture.get(number)
        if lecture_units and not (units & lecture_units):
            changes.append({"number": number, "note": sorted(units), "lecture": sorted(lecture_units)})
    return changes


# --------------------------------------------------------------------------------------
# Exam statements need a cue
# --------------------------------------------------------------------------------------


def cue_stem_sets(chunk_texts: list[str]) -> list[set[str]]:
    """Four-letter stems of every exam, importance, assignment, deadline and review cue sentence."""
    stems: list[set[str]] = []
    for text in chunk_texts:
        for cue in find_cues(str(text or ""), limit=30):
            stems.append({token[:4] for token in tokenize(cue.sentence) if len(token) >= 4})
    return stems


def exam_statement_is_backed(sentence: str, cue_stems: list[set[str]]) -> bool:
    """True when a cue of the lecture shares at least three stems with the statement (all, for a short one)."""
    if _classify(sentence) in {KIND_ASSIGNMENT, KIND_DEADLINE}:
        return True  # quizzes, homework and deadlines are always kept as they are
    stems = {token[:4] for token in tokenize(sentence) if len(token) >= 4}
    if not stems:
        return True
    need = min(3, len(stems))
    return any(len(stems & other) >= need for other in cue_stems)


def demote_unbacked_exam_statements(
    statements: list[tuple[str, str]], cue_stems: list[set[str]]
) -> tuple[list[tuple[str, str]], list[str]]:
    """(statements that keep the exam mark, sentences to show as plain notes)."""
    kept: list[tuple[str, str]] = []
    demoted: list[str] = []
    for status, sentence in statements:
        if exam_statement_is_backed(sentence, cue_stems):
            kept.append((status, sentence))
        else:
            demoted.append(sentence)
    return kept, demoted
