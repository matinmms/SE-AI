"""Words a lecturer says in another language than the lecture's own (Arabic, English, French, ...).

A Persian lecture often carries Arabic terms and quotations (literature, religion, law, philosophy)
or English terms (computing, medicine, science). The note must keep each of them in the language and
script the lecturer used. This module finds them in a transcript (Latin-script terms and
Arabic-script terms) and reports which ones a note lost. It is pure and free: no model call, used by
the writer prompt hints (revision 3) and by the QA automatic checks.

Arabic is told apart from Persian by letters and marks Persian does not use: the vowel marks
(harakat), tā' marbūṭa «ة», alef maqṣūra «ى», Arabic yā «ي» / kāf «ك» (which a recogniser sometimes
keeps), and a small set of Arabic phrases that open quotations.
"""
from __future__ import annotations

import re
from typing import Any

_LATIN_TERM = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9+#]*(?:[-/.'][A-Za-z0-9+#]+)*"
    r"(?: [A-Za-z][A-Za-z0-9+#]*(?:[-/.'][A-Za-z0-9+#]+)*){0,3}(?![A-Za-z0-9_])"
)
_ARABIC_LETTER = r"ء-ؿف-يٱ-ۓً-ْٰـ"
# Marks Persian does not use. The tanwin-fatha is left out on purpose: Persian writes it too
# (کاملاً، مثلاً، حتماً).
_ARABIC_MARKS = re.compile(r"[\u064C-\u0652\u0670\u0629\u0649\u064A\u0643]")
# Particles and function words that only Arabic uses; a run of words holding one of these is Arabic
# even when the recogniser wrote no vowel marks.
_ARABIC_PARTICLES = frozenset(
    "في على إلى الى عن الذي التي هذا هذه ذلك هي لم لن أن إن كان كانت قال قالت يقول بسم الحمد لله رب العالمين الرحمن الرحيم".split()
)
_NOT_ARABIC_AL = frozenset({"الگوریتم", "الکترون", "الکل", "الان", "الکترونیک", "الگو", "الفبا", "الکتریکی", "الکتریسیته"})
_ARABIC_WORD = re.compile(f"[{_ARABIC_LETTER}]+")
_ZWNJ = "‌"
MAX_TERMS = 60


def _norm(term: str) -> str:
    text = str(term or "").replace(_ZWNJ, " ").casefold()
    text = re.sub(r"[ً-ْٰـ]", "", text).replace("ي", "ی").replace("ك", "ک").replace("ى", "ی")
    return re.sub(r"\s+", " ", text).strip(" .,;:!?؟،؛()[]{}«»\"'-")


def latin_terms(text: str, *, limit: int = MAX_TERMS) -> list[str]:
    """Latin-script words and short phrases, first-seen order (single letters and bare digits skipped)."""
    seen: set[str] = set()
    terms: list[str] = []
    for match in _LATIN_TERM.finditer(str(text or "")):
        term = match.group(0).strip(" .-/'")
        if len(term) < 2 or re.fullmatch(r"[A-Za-z]\d*", term):
            continue
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
        if len(terms) >= limit:
            break
    return terms


def _arabic_like(word: str) -> bool:
    """Strong evidence the word is Arabic: an Arabic-only mark, or an Arabic particle."""
    return bool(_ARABIC_MARKS.search(word)) or word in _ARABIC_PARTICLES


def _arabic_neighbour(word: str) -> bool:
    return _arabic_like(word) or (word.startswith("ال") and len(word) >= 5 and word not in _NOT_ARABIC_AL)


_COMMON_LATIN = frozenset({"the", "and", "for", "with", "not", "are", "was", "this", "that", "from", "you", "your", "but", "can", "will", "all", "one", "two"})


def latin_words(text: str, *, limit: int = MAX_TERMS) -> list[str]:
    """Single Latin-script words of three or more letters (or any acronym), first-seen order."""
    seen: set[str] = set()
    words: list[str] = []
    for match in re.finditer(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9+#]*(?![A-Za-z0-9_])", str(text or "")):
        word = match.group(0)
        key = word.casefold()
        if (len(word) < 3 and not word.isupper()) or key in _COMMON_LATIN or key in seen:
            continue
        seen.add(key)
        words.append(word)
        if len(words) >= limit:
            break
    return words


def arabic_terms(text: str, *, limit: int = MAX_TERMS) -> list[str]:
    """Arabic words and phrases, first-seen order.

    A run starts at a word with an Arabic-only mark or an Arabic particle and extends over the
    neighbouring words that look Arabic (an «ال» definite article counts only beside such a word). A
    lone particle such as «من» or «ما» is not reported on its own: Persian shares those spellings, so a
    run needs two Arabic-looking words or one word with an Arabic-only mark.
    """
    seen: set[str] = set()
    terms: list[str] = []
    words = [re.sub(r"^[^\u0600-\u06FF]+|[^\u0600-\u06FF]+$", "", w) for w in re.findall(r"\S+", str(text or "").replace(_ZWNJ, " "))]
    index = 0
    while index < len(words) and len(terms) < limit:
        word = words[index]
        if word and _ARABIC_WORD.fullmatch(word) and _arabic_like(word):
            run = [word]
            cursor = index + 1
            while cursor < len(words) and len(run) < 8:
                nxt = words[cursor]
                if nxt and _ARABIC_WORD.fullmatch(nxt) and _arabic_neighbour(nxt):
                    run.append(nxt)
                    cursor += 1
                else:
                    break
            strong = any(_ARABIC_MARKS.search(w) for w in run)
            if strong or len(run) >= 2:
                phrase = " ".join(run)
                key = _norm(phrase)
                if len(key) >= 3 and key not in seen:
                    seen.add(key)
                    terms.append(phrase)
            index = cursor
        else:
            index += 1
    return terms


def foreign_terms(text: str, *, limit: int = MAX_TERMS) -> dict[str, list[str]]:
    return {"latin": latin_terms(text, limit=limit), "arabic": arabic_terms(text, limit=limit)}


def foreign_terms_hint(text: str, *, limit: int = 40) -> str:
    """The prompt line that names the foreign words in these excerpts, so none is translated away.

    Empty when there are none. The Latin part is also given by the older English-term hint; this one
    adds the Arabic part and states the rule for every language.
    """
    found = foreign_terms(text, limit=limit)
    parts: list[str] = []
    if found["arabic"]:
        parts.append(f"Arabic words and phrases the lecturer said (keep each in Arabic script, exactly as written here, with no translation in its place): {found['arabic']}")
    if found["latin"]:
        parts.append(f"Words in Latin letters the lecturer said (keep each in Latin letters, exactly as written here): {found['latin']}")
    if not parts:
        return ""
    return " ".join(parts) + ". A foreign word the transcript spelled phonetically in Persian script counts too: write its real spelling.\n"


def missing_foreign_terms(transcript_text: str, note_text: str, *, limit: int = MAX_TERMS) -> dict[str, Any]:
    """Foreign terms found in the transcript that no longer appear in the note.

    Returns {"total": n, "missing": [terms], "by_script": {"latin": [..], "arabic": [..]}}.
    A term counts as kept when its normalised form occurs anywhere in the normalised note text; for a
    multi-word Arabic phrase, kept when at least 60% of its words occur.
    """
    found = {"latin": latin_words(transcript_text, limit=limit), "arabic": arabic_terms(transcript_text, limit=limit)}
    note_norm = _norm(note_text)
    note_words = set(note_norm.split())
    missing: dict[str, list[str]] = {"latin": [], "arabic": []}
    total = 0
    for script, terms in found.items():
        for term in terms:
            total += 1
            key = _norm(term)
            if not key:
                continue
            if key in note_norm:
                continue
            words = key.split()
            if script == "arabic" and len(words) > 1 and sum(1 for w in words if w in note_words) / len(words) >= 0.6:
                continue
            missing[script].append(term)
    return {"total": total, "missing": missing["latin"] + missing["arabic"], "by_script": missing}
