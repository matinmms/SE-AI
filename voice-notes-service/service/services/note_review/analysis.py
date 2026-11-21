"""Pure analysis for the note accuracy check: no database, no storage, no network.

Pipeline: note sections + speech segments -> monotonic alignment (which section does each
spoken segment belong to) -> per-window badness -> a short ranked list of passages to re-listen to.

Everything here is deterministic and testable. Nothing is guessed: a passage whose alignment to
the note is weak is dropped, never attached to a heading.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

_ARABIC_DIACRITICS = re.compile("[ً-ٰٟۖ-ۭ]")
_TAGS = re.compile(r"<[^>]+>")
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_DIGIT_TABLE = {ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGIT_TABLE.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})

_SKIP_KEYS = {
    "type",
    "anchor_id",
    "variants",
    "class_name",
    "number",
    "accent_dot",
    "src",
    "url",
    "href",
    "image_id",
    "id",
}

# Phrases Whisper-family models emit over music or noise.
_HALLUCINATION_MARKERS = (
    "موسیقی",
    "زیرنویس",
    "ممنون که تماشا کردید",
    "مرسی که تماشا کردید",
    "از تماشای شما سپاسگزارم",
    "اشتراک کانال",
    "amara.org",
)


def normalize_text(text: str) -> str:
    text = str(text or "").replace("‌", " ").replace("ي", "ی").replace("ك", "ک")
    text = _ARABIC_DIACRITICS.sub("", text).translate(_DIGIT_TABLE).lower()
    return _NON_WORD.sub(" ", text)


def tokenize(text: str) -> list[str]:
    return [token for token in normalize_text(text).split() if len(token) >= 2 or token.isdigit()]


def _plain(value: Any) -> str:
    if isinstance(value, str):
        return _TAGS.sub(" ", value)
    if isinstance(value, list):
        return " ".join(_plain(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_plain(item) for key, item in value.items() if key not in _SKIP_KEYS)
    return ""


@dataclass
class Section:
    index: int
    heading: str
    text: str = ""
    tokens: set[str] = field(default_factory=set)


def extract_sections(semantic_payload: dict[str, Any] | None) -> list[Section]:
    """Sections are the runs of blocks that start at a `section_header` block."""
    pages = (semantic_payload or {}).get("pages")
    if not isinstance(pages, list):
        return []
    sections: list[Section] = []
    preface: list[str] = []
    for page in pages:
        blocks = page.get("blocks") if isinstance(page, dict) else None
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            # Stored notes use hyphens ("section-header"); the renderer treats both spellings alike.
            if str(block.get("type", "")).replace("-", "_") == "section_header":
                heading = " ".join(_plain(block.get("title", "")).split())
                sections.append(Section(index=len(sections), heading=heading, text=heading))
                continue
            text = " ".join(_plain(block).split())
            if not text:
                continue
            if sections:
                sections[-1].text += " " + text
            else:
                preface.append(text)
    if sections and preface:
        sections[0].text = sections[0].text + " " + " ".join(preface)
    for section in sections:
        section.tokens = set(tokenize(section.text))
    return sections


@dataclass
class SegmentInfo:
    index: int
    window_index: int
    start: float
    end: float
    text: str
    avg_logprob: float | None
    no_speech_prob: float | None
    compression_ratio: float | None
    tokens: list[str] = field(default_factory=list)


def align_segments(
    segments: list[SegmentInfo],
    sections: list[Section],
    *,
    switch_penalty: float = 0.12,
    smooth_radius: int = 3,
) -> tuple[list[int], list[float]]:
    """Monotonic assignment of segments to sections.

    Returns (section index per segment, smoothed alignment score per segment). The score is the
    mean match density over the neighbouring segments, so a badly heard segment (whose own words
    do not match the note) still inherits the confidence of the well-matched speech around it.
    """
    n, j_count = len(segments), len(sections)
    if n == 0 or j_count == 0:
        return [], []

    document_frequency: dict[str, int] = {}
    for section in sections:
        for token in section.tokens:
            document_frequency[token] = document_frequency.get(token, 0) + 1

    def weight(token: str) -> float:
        # Words that appear in every section carry no location information.
        return math.log((j_count + 1) / (1 + document_frequency.get(token, 0))) + 0.05

    raw: list[list[float]] = []
    for segment in segments:
        total = sum(weight(token) for token in segment.tokens)
        row = []
        for section in sections:
            if total <= 0:
                row.append(0.0)
                continue
            hit = sum(weight(token) for token in segment.tokens if token in section.tokens)
            row.append(hit / total)
        raw.append(row)

    neg = float("-inf")
    dp = [[neg] * j_count for _ in range(n)]
    back = [[0] * j_count for _ in range(n)]
    dp[0] = list(raw[0])
    for i in range(1, n):
        best_value, best_index = neg, 0
        for k in range(j_count):
            stay = dp[i - 1][k]
            # Best predecessor strictly before k (moving forward in the note), paying the switch penalty.
            if k > 0 and dp[i - 1][k - 1] > best_value:
                best_value, best_index = dp[i - 1][k - 1], k - 1
            switch = best_value - switch_penalty if best_value > neg else neg
            if switch > stay:
                dp[i][k] = raw[i][k] + switch
                back[i][k] = best_index
            else:
                dp[i][k] = raw[i][k] + stay
                back[i][k] = k
    k = max(range(j_count), key=lambda idx: dp[-1][idx])
    assigned = [0] * n
    for i in range(n - 1, -1, -1):
        assigned[i] = k
        k = back[i][k]

    own = [raw[i][assigned[i]] for i in range(n)]
    smoothed = []
    for i in range(n):
        low, high = max(0, i - smooth_radius), min(n, i + smooth_radius + 1)
        smoothed.append(sum(own[low:high]) / (high - low))
    return assigned, smoothed


@dataclass
class Unit:
    """Consecutive segments from one recognizer decode window (they share its statistics)."""

    window_index: int
    segments: list[SegmentInfo]
    start: float = 0.0
    end: float = 0.0
    badness: float = 0.0
    reasons: list[str] = field(default_factory=list)
    section_index: int = 0
    alignment: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def text(self) -> str:
        return " ".join(segment.text for segment in self.segments if segment.text).strip()


def build_units(segments: list[SegmentInfo]) -> list[Unit]:
    units: list[Unit] = []
    for segment in segments:
        if units and units[-1].window_index == segment.window_index:
            units[-1].segments.append(segment)
        else:
            units.append(Unit(window_index=segment.window_index, segments=[segment]))
    for unit in units:
        unit.start = min(segment.start for segment in unit.segments)
        unit.end = max(segment.end for segment in unit.segments)
    return units


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


# Sped-up, silence-cut audio inflates Whisper's no-speech probability: on the algorithms lecture
# 384 of 812 segments with text scored above 0.5. When that many do, the signal says nothing
# about a particular passage and is ignored for the lecture.
NO_SPEECH_UNRELIABLE_SHARE = 0.25
# Alignment of a colloquial, partly garbled transcript to a written note is low everywhere
# (median 0.10 there). A passage only needs to be placed as well as the lower quarter of its lecture.
MIN_RELATIVE_ALIGNMENT = 0.08


def no_speech_is_reliable(segments: list[SegmentInfo]) -> bool:
    spoken = [segment for segment in segments if segment.text.strip()]
    if not spoken:
        return True
    high = sum(1 for segment in spoken if (segment.no_speech_prob or 0.0) > 0.5)
    return high / len(spoken) <= NO_SPEECH_UNRELIABLE_SHARE


def score_unit(unit: Unit, *, use_no_speech: bool = True) -> None:
    """Fill `badness` (0 = clear, 1 = surely worth re-listening) and `reasons`."""
    first = unit.segments[0]
    reasons: list[tuple[str, float]] = []
    tokens = [token for segment in unit.segments for token in segment.tokens]
    text_empty = not tokens

    lp, nsp, cr = first.avg_logprob, first.no_speech_prob, first.compression_ratio
    if text_empty and (nsp is None or nsp >= 0.5):
        # Silence: nothing to listen to, and not the recognizer's fault.
        unit.badness, unit.reasons = 0.0, []
        return
    if lp is not None:
        reasons.append(("LOW_LOGPROB", _clamp((-lp - 0.35) / 0.5)))
    if nsp is not None and not text_empty and use_no_speech:
        reasons.append(("NO_SPEECH", _clamp((nsp - 0.4) / 0.4) * 0.9))
    if cr is not None:
        reasons.append(("HIGH_COMPRESSION", _clamp((cr - 2.2) / 0.8)))

    trigrams = [tuple(tokens[i : i + 3]) for i in range(len(tokens) - 2)]
    if len(trigrams) >= 6:
        repeated = (len(trigrams) - len(set(trigrams))) / len(trigrams)
        reasons.append(("REPEAT", _clamp((repeated - 0.3) / 0.4)))

    duration = unit.duration
    if duration >= 8 and (nsp is None or nsp < 0.5):
        words_per_second = len(tokens) / duration
        if words_per_second < 0.5:
            reasons.append(("SPARSE_TEXT", _clamp((0.5 - words_per_second) / 0.4) * 0.8))

    normalized = normalize_text(unit.text)
    if len(tokens) <= 8 and any(marker in normalized for marker in _HALLUCINATION_MARKERS):
        reasons.append(("KNOWN_HALLUCINATION", 1.0))

    unit.badness = max((value for _, value in reasons), default=0.0)
    unit.reasons = [code for code, value in sorted(reasons, key=lambda item: -item[1]) if value >= 0.2]


@dataclass
class Spot:
    unit: Unit
    section_index: int
    badness: float
    alignment: float
    reasons: list[str]
    # Where the passage sits inside its section, 0 (start) to 1 (end), from the section's speaking time.
    section_fraction: float | None = None


def section_spans(segments: list[SegmentInfo], assigned: list[int]) -> dict[int, tuple[float, float]]:
    """The speaking time each section covers: (first segment start, last segment end)."""
    spans: dict[int, tuple[float, float]] = {}
    for segment in segments:
        section = assigned[segment.index]
        low, high = spans.get(section, (segment.start, segment.end))
        spans[section] = (min(low, segment.start), max(high, segment.end))
    return spans


def fraction_in_span(start: float, span: tuple[float, float] | None) -> float | None:
    if span is None:
        return None
    low, high = span
    if high - low <= 0:
        return 0.0
    return round(min(1.0, max(0.0, (start - low) / (high - low))), 4)


def select_spots(
    units: list[Unit],
    *,
    min_badness: float,
    min_alignment: float,
    max_spots: int,
    max_ratio: float,
) -> tuple[list[Spot], dict[str, int]]:
    """Worst windows by rank, capped, and only those that can be placed under a heading."""
    candidates = [unit for unit in units if unit.badness >= min_badness]
    placed = [unit for unit in candidates if unit.alignment >= min_alignment]
    cap = min(max_spots, max(1, math.ceil(max_ratio * max(1, len(units)))))
    chosen = sorted(placed, key=lambda unit: -unit.badness)[:cap]
    chosen.sort(key=lambda unit: unit.start)
    spots = [
        Spot(unit=unit, section_index=unit.section_index, badness=unit.badness, alignment=unit.alignment, reasons=unit.reasons)
        for unit in chosen
    ]
    stats = {
        "units": len(units),
        "candidates": len(candidates),
        "unaligned_candidates": len(candidates) - len(placed),
        "chosen": len(spots),
    }
    return spots, stats


def clear_ratio(units: Iterable[Unit], *, min_badness: float) -> float | None:
    total = sum(unit.duration for unit in units)
    if total <= 0:
        return None
    clear = sum(unit.duration for unit in units if unit.badness < min_badness)
    return round(clear / total, 4)


def analyze(
    segments: list[SegmentInfo],
    sections: list[Section],
    *,
    min_badness: float,
    min_alignment: float,
    max_spots: int,
    max_ratio: float,
) -> tuple[list[Spot], list[Unit], dict[str, int]]:
    for segment in segments:
        segment.tokens = tokenize(segment.text)
    assigned, smoothed = align_segments(segments, sections)
    units = build_units(segments)
    use_no_speech = no_speech_is_reliable(segments)
    for unit in units:
        score_unit(unit, use_no_speech=use_no_speech)
        weights: dict[int, float] = {}
        alignment_total = 0.0
        for segment in unit.segments:
            duration = max(0.001, segment.end - segment.start)
            weights[assigned[segment.index]] = weights.get(assigned[segment.index], 0.0) + duration
            alignment_total += smoothed[segment.index]
        unit.section_index = max(weights, key=weights.get) if weights else 0
        unit.alignment = alignment_total / len(unit.segments)
    alignments = sorted(unit.alignment for unit in units)
    # The lower quartile: a passage placed at least as well as a quarter of its lecture.
    typical = alignments[len(alignments) // 4] if alignments else min_alignment
    effective_alignment = max(MIN_RELATIVE_ALIGNMENT, min(min_alignment, typical))
    spots, stats = select_spots(
        units,
        min_badness=min_badness,
        min_alignment=effective_alignment,
        max_spots=max_spots,
        max_ratio=max_ratio,
    )
    spans = section_spans(segments, assigned)
    for spot in spots:
        spot.section_fraction = fraction_in_span(spot.unit.start, spans.get(spot.section_index))
    stats = {**stats, "no_speech_used": int(use_no_speech), "min_alignment_used": round(effective_alignment, 3)}
    return spots, units, stats
