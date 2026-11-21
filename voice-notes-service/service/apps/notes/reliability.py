"""How much of a stretch of transcript is real speech, judged from the text alone.

When the audio is damaged (noise, a far microphone, a heavy accent) speech-to-text prints near-words
that are not Persian. The QA reviews found notes written almost entirely from general knowledge on
top of such transcripts. Ordinary Persian lecture speech is full of small function words («و»،
«که»، «را»، «این»، «از»، «در»، «به»، «است»...): about a quarter of the tokens. Near-word garbage has
almost none, so a low share of them marks a stretch as mostly unintelligible. A cheap, free
heuristic (no model call); it is only used to tell the writer to say little and to give the QA
reviewer a number, never to drop text.
"""

from __future__ import annotations

import re

MIN_TOKENS = 40  # fewer Persian tokens than this is too little text to judge
SEVERE_SHARE = 0.10
PARTIAL_SHARE = 0.16

_FUNCTION_WORDS = frozenset(
    "و که را این از در به با است هم یک آن برای می های هست بود شد یا تا اگر پس چون ولی اما خب هر همین اون "
    "باید کنیم میشه نه بعد وقتی چه چی ما شما من او اینه اونه یعنی هستند بشه کنه داره دارد شده بین روی "
    "همه خیلی هیچ ولی بدون مثل هستش بود‌ه کرد کنید می‌شود می‌کند".split()
)
_PERSIAN_TOKEN = re.compile(r"[؀-ۿ‌]+")


def function_word_share(text: str) -> tuple[int, float]:
    """(number of Persian-script tokens, share of them that are function words)."""
    tokens = [token.strip("‌") for token in _PERSIAN_TOKEN.findall(str(text or ""))]
    tokens = [token for token in tokens if token]
    if not tokens:
        return 0, 0.0
    hits = sum(1 for token in tokens if token in _FUNCTION_WORDS)
    return len(tokens), hits / len(tokens)


def garble_level(text: str) -> str:
    """"severe", "partial" or "" (clear, or too short to judge)."""
    count, share = function_word_share(text)
    if count < MIN_TOKENS:
        return ""
    if share < SEVERE_SHARE:
        return "severe"
    if share < PARTIAL_SHARE:
        return "partial"
    return ""


def damaged_excerpt_prompt(text: str) -> str:
    """A prompt block for the writer when these excerpts look mostly unintelligible ('' otherwise)."""
    level = garble_level(text)
    if not level:
        return ""
    if level == "severe":
        return (
            "DAMAGED EXCERPTS: these excerpts are mostly unintelligible speech-to-text (near-words, not "
            "sentences). Write ONLY what is clearly and completely stated in them: short, plain, "
            "sentence by sentence. Add nothing from general knowledge, no steps, definitions, examples, "
            "numbers, names or algorithms that are not clearly in the text; leave a topic out when only "
            "its name is audible, and make the unit and the note short rather than complete. Never "
            "say that the audio was unclear.\n"
        )
    return (
        "PARTLY DAMAGED EXCERPTS: part of these excerpts is unintelligible speech-to-text. Write each "
        "clearly stated point; where a stretch is near-words, say nothing about it and add nothing "
        "from general knowledge (no steps, numbers, names, abbreviations or examples that are not "
        "clearly in the text). Never say that the audio was unclear.\n"
    )


def garbled_chunk_share(chunks: list[dict]) -> float | None:
    """Share of transcript chunks (by words) that are severely garbled, or None when nothing could be judged."""
    judged = bad = 0
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        count, _ = function_word_share(text)
        if count < 15:
            continue
        judged += count
        _, share = function_word_share(text)
        if share < SEVERE_SHARE:
            bad += count
    return round(bad / judged, 3) if judged else None


MAX_DAMAGE_MARKS = 6


def damaged_chunks_prompt(chunk_texts: list[str]) -> str:
    """A notice naming the stretches of these excerpts that the recognizer broke ('' when none).

    The excerpts reach the writer as chunks joined by blank lines, so each broken stretch is named
    by its opening words; the notice sits in the prompt header, never in the text, so it cannot
    leak into the note. Free, no model call. The excerpt-level `damaged_excerpt_prompt` still
    applies when the whole excerpt is broken.
    """
    marks: list[tuple[str, str]] = []
    for text in chunk_texts:
        level = garble_level(text)
        if not level:
            continue
        opening = " ".join(str(text or "").split()[:6])
        if opening:
            marks.append((level, opening))
    if not marks:
        return ""
    shown = marks[:MAX_DAMAGE_MARKS]
    listed = "; ".join(
        f"«{opening}…» ({'mostly unintelligible' if level == 'severe' else 'partly unintelligible'})"
        for level, opening in shown
    )
    more = f" (and {len(marks) - len(shown)} more)" if len(marks) > len(shown) else ""
    return (
        "BROKEN STRETCHES: in these excerpts the speech-to-text broke the stretches that open with: "
        f"{listed}{more}. From such a stretch take no term, name, abbreviation, English word or figure; "
        "write only what the clear parts around it state. Never say that the audio was unclear.\n"
    )
