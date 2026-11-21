"""The context prompt sent with every speech-to-text request (Whisper ``prompt``).

Why: the recogniser is forced to Persian (``language=fa``). Without any hint it writes English terms
phonetically in Persian script and often misses Arabic phrases and grammar words, so a literature
student's Arabic terms and a medical student's English terms were lost before the note was written.
A short natural sentence that says "this is a Persian lecture that also contains Arabic and English
words, written in their own script", followed by the terms this lecture is likely to use, steers the
recogniser toward the right spelling and script. No paid call is made to build it: it uses the
course name, the title, the slide text and small built-in term lists per field.

The prompt is capped (Whisper only reads about 224 tokens) and never carries user text beyond the
course name, the title and terms found on the user's own slides.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from django.conf import settings

MAX_PROMPT_CHARS = 360
MAX_TERM_CHARS = 28
MAX_SLIDE_TERMS = 10

_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[-/.'][A-Za-z0-9+#]+)*")
# Arabic-only letters and harakat: a word with one of these is Arabic, not Persian.
_ARABIC_MARKS = re.compile(r"[ً-ْـةيكىۃ]")
_TOKEN = re.compile(r"[\w؀-ۿ+#/.-]{3,}", re.UNICODE)

# field keywords (Persian and English, lower-cased) -> terms a lecturer of that field says in the
# original language. Short on purpose: they only seed the recogniser, the lecture's own terms come first.
_FAMILIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("عربی", "ادبیات", "نحو", "صرف", "بلاغت", "قرآن", "حدیث", "فقه", "تفسیر", "متون", "عروض", "کلام", "منطق", "arabic"),
        ("فاعل", "مفعول", "مبتدا", "خبر", "إعراب", "مرفوع", "منصوب", "مجرور", "فعل ماضی", "مضارع", "بسم الله الرحمن الرحیم", "قال الله تعالی"),
    ),
    (
        ("کامپیوتر", "برنامه", "الگوریتم", "داده", "شبکه", "هوش مصنوعی", "یادگیری", "نرم‌افزار", "نرم افزار", "سیستم عامل", "پایگاه", "computer", "software", "algorithm", "network", "database", "machine learning"),
        ("algorithm", "database", "API", "thread", "overfitting", "machine learning", "pipeline"),
    ),
    (
        ("پزشکی", "آناتومی", "فیزیولوژی", "داروسازی", "فارماکولوژی", "پرستاری", "بیوشیمی", "ژنتیک", "زیست", "میکروب", "ایمنی", "پاتولوژی", "دندان", "medical", "anatomy", "biology", "genetic", "pharmacology"),
        ("DNA", "RNA", "enzyme", "receptor", "pH", "mRNA", "PCR"),
    ),
    (
        ("مهندسی", "برق", "مدار", "الکترونیک", "مکانیک", "کنترل", "سیگنال", "ریزپردازنده", "ریاضی", "فیزیک", "آمار", "engineering", "circuit", "signal", "control"),
        ("CPU", "ADC", "feedback", "bandwidth", "Fourier", "transfer function"),
    ),
    (
        ("حسابداری", "مالی", "اقتصاد", "مدیریت", "بازاریابی", "بانک", "بیمه", "accounting", "finance", "economics", "management", "marketing"),
        ("IFRS", "ROI", "cash flow", "marketing", "SWOT", "GDP"),
    ),
    (
        ("انگلیسی", "زبان", "ترجمه", "english", "grammar", "linguistic", "زبان‌شناسی"),
        ("grammar", "present perfect", "vocabulary", "reading", "listening"),
    ),
    (
        ("روانشناسی", "جامعه‌شناسی", "جامعه شناسی", "تربیتی", "فلسفه", "حقوق", "psychology", "sociology", "philosophy", "law"),
        ("cognitive", "behaviorism", "Freud", "paradigm"),
    ),
)

_SENTENCE = (
    "این یک جلسهٔ درس دانشگاهی به زبان فارسی است. استاد واژه‌ها و جمله‌های عربی و انگلیسی و اصطلاحات "
    "تخصصی را همان‌طور که می‌گوید، به زبان و خط اصلی خودشان می‌نویسد."
)


def stt_context_prompt_enabled() -> bool:
    return bool(getattr(settings, "STT_CONTEXT_PROMPT_ENABLED", False))


def _clean_term(term: str) -> str:
    term = re.sub(r"\s+", " ", str(term or "").replace("‌", " ")).strip(" .,;:()[]{}«»\"'-")
    return term[:MAX_TERM_CHARS].strip()


def _is_foreign_term(term: str) -> bool:
    return bool(_LATIN_WORD.fullmatch(term.split(" ")[0]) if term else False) or bool(_ARABIC_MARKS.search(term))


def _slide_terms(lecture: Any) -> list[str]:
    """The foreign-looking words that recur on the user's own slides, most frequent first."""
    try:
        pages = list(lecture.slide_pages.all().order_by("page_number")[:60])
    except Exception:  # a lightweight stand-in or an unsaved lecture has no slides
        return []
    counts: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    for page in pages:
        text = " ".join(str(getattr(page, name, "") or "") for name in ("text", "structured_text", "ocr_text", "raw_extracted_text"))
        for token in _TOKEN.findall(text):
            term = _clean_term(token)
            if len(term) < 3 or not _is_foreign_term(term) or term.isdigit():
                continue
            key = term.casefold()
            counts[key] += 1
            spelling.setdefault(key, term)
    ranked = sorted(counts, key=lambda key: (-counts[key], list(spelling).index(key)))
    return [spelling[key] for key in ranked[:MAX_SLIDE_TERMS]]


def _own_terms(lecture: Any) -> list[str]:
    """Foreign words already present in the course name, title and professor."""
    found: list[str] = []
    for value in (getattr(lecture, "course_name", ""), getattr(lecture, "title", "")):
        found.extend(_LATIN_WORD.findall(str(value or "")))
    return [t for t in (_clean_term(t) for t in found) if len(t) >= 2]


def _family_terms(lecture: Any) -> list[str]:
    haystack = f"{getattr(lecture, 'course_name', '')} {getattr(lecture, 'title', '')}".casefold().replace("‌", " ")
    terms: list[str] = []
    for keywords, seeds in _FAMILIES:
        if any(keyword in haystack for keyword in keywords):
            terms.extend(seeds)
    return terms


def build_stt_context_prompt(lecture: Any) -> str:
    """One prompt string for every chunk of this lecture, or "" when no prompt should be sent."""
    language = str(getattr(lecture, "input_language", "") or "fa").strip().lower()
    course = _clean_term(getattr(lecture, "course_name", ""))
    title = _clean_term(getattr(lecture, "title", ""))
    terms: list[str] = []
    seen: set[str] = set()
    for term in (*_own_terms(lecture), *_slide_terms(lecture), *_family_terms(lecture)):
        key = term.casefold()
        if key and key not in seen:
            seen.add(key)
            terms.append(term)

    if language != "fa":
        # Another lecture language: only the plain terms, no Persian instruction text.
        return ", ".join(terms)[:MAX_PROMPT_CHARS]

    head = f"درس «{course}»." if course and course != title else ""
    if title and title != course:
        head = f"{head} موضوع: «{title}».".strip()
    body = _SENTENCE
    prompt = f"{head} {body}".strip()
    if terms:
        tail = "، ".join(terms)
        prompt = f"{prompt} {tail}"
    if len(prompt) > MAX_PROMPT_CHARS:
        # keep the sentence, trim whole terms from the end
        room = MAX_PROMPT_CHARS - len(f"{head} {body}".strip()) - 1
        kept: list[str] = []
        used = 0
        for term in terms:
            extra = len(term) + (2 if kept else 0)
            if used + extra > room:
                break
            kept.append(term)
            used += extra
        prompt = f"{head} {body}".strip() + (" " + "، ".join(kept) if kept else "")
    return prompt[:MAX_PROMPT_CHARS].strip()


def strip_prompt_echo(text: str, prompt: str | None) -> str:
    """Whisper sometimes repeats its prompt (usually on silence). Remove that so it never reaches a note."""
    text = str(text or "").strip()
    if not text or not prompt:
        return text
    sentence = " ".join(_SENTENCE.split())
    out = " ".join(text.split())
    whole = " ".join(str(prompt).split())
    if whole in out:
        out = out.replace(whole, " ")
    if sentence in out:
        out = out.replace(sentence, " ")
    head_match = re.match(r"^\s*درس «[^»]{0,60}»\.(?: موضوع: «[^»]{0,60}»\.)?\s*", out)
    if head_match and (sentence in " ".join(text.split()) or len(out) < 60):
        out = out[head_match.end():]
    tail = " ".join(str(prompt).split())
    for size in (80, 40):
        probe = tail[-size:]
        if len(probe) == size and probe in out:
            out = out.replace(probe, " ")
    return re.sub(r"\s+", " ", out).strip()
