"""Remarks a lecturer makes about the course itself: quizzes, exams, homework, deadlines, stressed points.

These sentences are the part of a lecture a student acts on, and they were the part the note QA
reviews found missing most often (a quiz announcement, a submission deadline, «remember these
three», an open-book rule). The writer is told about them before it writes a part, and the QA
checks use the same finder to see whether the finished note kept them, so what the writer is asked
to keep and what is measured can never drift apart.

Pure functions only: no Django, no model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

KIND_EXAM = "exam"
KIND_ASSIGNMENT = "assignment"
KIND_DEADLINE = "deadline"
KIND_IMPORTANCE = "importance"

MAX_SENTENCE_CHARS = 240
MIN_SENTENCE_CHARS = 14
MAX_CUES_PER_TEXT = 8
_KIND_ORDER = (KIND_EXAM, KIND_ASSIGNMENT, KIND_DEADLINE, KIND_IMPORTANCE)

_FA_EDGE = r"(?<![؀-ۿ‌])"
_FA_END = r"(?![؀-ۿ])"

# Speech-to-text spells «کوئیز» several ways and writes «تأخیر» as «تخخیر».
_EXAM = re.compile(
    r"امتحان|میان[‌ ]?ترم|پایان[‌ ]?ترم|کو[ئی]{1,2}ز|\bquiz\b|\bexam\b|\bmidterm\b",
    re.IGNORECASE,
)
# «امتحان کنید» means «try it», not an exam.
_EXAM_TRY = re.compile(r"امتحان\s*(?:کن|کرد|می[‌ ]?کن|شد|می[‌ ]?شد)")
_ASSIGNMENT_NOUN = re.compile(r"تمرین|تکلیف|پروژه|گزارش|\bhomework\b|\bassignment\b", re.IGNORECASE)
_ASSIGNMENT_ACTION = re.compile(
    r"بنویس|حل\s*کن|انجام\s*(?:بد|بده)|تحویل|آپلود|اپلود|بفرست|بیار|معرفی\s*کن|"
    r"بخوان|مطالعه\s*کن|پیاده[‌ ]?سازی\s*کن|جنریت|آماده\s*کن|باید\s*بد",
)
_NEXT_SESSION = re.compile(r"(?:جلسه|هفته)\s*(?:ی\s*)?(?:بعد|آینده)")
_NEXT_SESSION_ACTION = re.compile(r"بیار|بنویس|معرفی|انجام|حل\s*کن|بخوان|آماده|تحویل|صحبت\s*کن")
_DEADLINE_WORD = re.compile(r"مهلت|ددلاین|\bdeadline\b|تحویل|آپلود|اپلود|ت[اأ]?خ{1,2}ی?ر", re.IGNORECASE)
_TIME_WORD = re.compile(rf"{_FA_EDGE}(?:روز|هفته|ساعت|فردا|امروز|ماه|تا){_FA_END}|\d")
_IMPORTANCE = re.compile(
    r"(?:یادتون|یادتان|یادت|یاد)\s*(?:باشه|باشد|بمونه|بماند)|فراموش\s*نکن|دقت\s*کن|"
    rf"{_FA_EDGE}(?:حتما|حتماً|مهمه){_FA_END}|مهم\s*است|نکته\s*(?:ی\s*)?مهم|خیلی\s*مهم|"
    r"نصف\s*(?:درس|کورس)|\bimportant\b|\bremember\b",
)
# «Write this down», «learn this»: the lecturer stresses an item without saying exam (2026-11-18 reviews).
# A step of a worked example that says «write …» has a formula or number in the sentence, so those are skipped.
_WRITE_DOWN = re.compile(
    r"یادداشت\s*کن(?:ید|ین)?|نوت\s*کن(?:ید|ین)?|"
    r"(?:این(?:\s*رو|\s*را)?|اینو)\s*(?:حتما\s*|حتماً\s*)?(?:بنویس(?:ید|ین)|یاد\s*بگیر(?:ید|ین)?|حفظ\s*کن(?:ید|ین)?)|"
    r"حتما\s*(?:یاد|حفظ)\s*(?:بگیر|کن)"
)
# Exam-review lectures: «this repeats», «the questions come from this slide», «this will come».
_REVIEW = re.compile(
    r"(?:تکرار|تکراری)\s*(?:می[‌ ]?شه|می[‌ ]?شود|شده|هست|است)|"
    r"(?:سوال|سؤال|سوالات|سؤالات|سوال[‌ ]?ها)[^.!?؟\n]{0,40}(?:از\s*(?:روی\s*)?(?:همین|این)?\s*(?:اسلاید|جزوه|فایل|پاورپوینت|کتاب)|می[‌ ]?(?:آد|یاد|آید)|میاد)|"
    r"(?:میاد|می[‌ ]?آید|می[‌ ]?آد|میان)\s*(?:تو|توی|در)\s*(?:امتحان|کوییز|کویز|آزمون)|"
    r"(?:از|روی)\s*(?:همین\s*)?(?:اسلاید|جزوه)[^.!?؟\n]{0,25}(?:می[‌ ]?دم|می[‌ ]?دن|میدن|می[‌ ]?دهم|می[‌ ]?دهند)|"
    r"(?:این|همین)\s*(?:سوال|مسئله|نکته)\s*(?:امتحانیه|امتحانی\s*است|می[‌ ]?آد|میاد)"
)
_SENTENCE_SPLIT = re.compile(r"[.!?؟۔\n]+")


@dataclass(frozen=True)
class Cue:
    kind: str
    sentence: str


def _classify(sentence: str) -> str:
    """The kind of course remark a sentence is, or ''."""
    if (_EXAM.search(sentence) and not _EXAM_TRY.search(sentence)) or _REVIEW.search(sentence):
        return KIND_EXAM
    if _ASSIGNMENT_NOUN.search(sentence) and _ASSIGNMENT_ACTION.search(sentence):
        return KIND_ASSIGNMENT
    if _NEXT_SESSION.search(sentence) and _NEXT_SESSION_ACTION.search(sentence):
        return KIND_ASSIGNMENT
    if _DEADLINE_WORD.search(sentence) and _TIME_WORD.search(sentence):
        return KIND_DEADLINE
    if _IMPORTANCE.search(sentence):
        return KIND_IMPORTANCE
    if _WRITE_DOWN.search(sentence) and not re.search(r"[=\d۰-۹]", sentence):
        return KIND_IMPORTANCE
    return ""


def find_cues(text: str, *, limit: int = MAX_CUES_PER_TEXT) -> list[Cue]:
    """The course remarks in a stretch of transcript, most actionable kinds first, without repeats."""
    found: dict[str, Cue] = {}
    for raw in _SENTENCE_SPLIT.split(str(text or "")):
        sentence = " ".join(raw.split())
        if not (MIN_SENTENCE_CHARS <= len(sentence) <= MAX_SENTENCE_CHARS):
            continue
        kind = _classify(sentence)
        if kind and sentence not in found:
            found[sentence] = Cue(kind=kind, sentence=sentence)
    ordered = sorted(found.values(), key=lambda cue: _KIND_ORDER.index(cue.kind))
    return ordered[: max(0, limit)]


def required_remarks_prompt(text: str) -> str:
    """A user-prompt block listing the remarks the writer must keep for these excerpts ('' when none)."""
    cues = find_cues(text)
    if not cues:
        return ""
    lines = "\n".join(f"- ({cue.kind}) {cue.sentence}" for cue in cues)
    return (
        "REMARKS THE LECTURER MADE IN THESE EXCERPTS (a quiz, an exam, homework, a deadline or a point he "
        "stressed; the wording below is raw speech-to-text, so clean it): keep every one, with its exact "
        "meaning, as its own remark in the unit where he said it -- an [[exam:important]] remark for a "
        "quiz, an exam or a stressed point, and the concrete items for homework or a deadline. Never drop "
        "one as class talk. Leave out any figure or name in it that is garbled.\n"
        f"{lines}\n"
    )


def _stems(text: str) -> set[str]:
    return {token[:4] for token in re.findall(r"[\w؀-ۿ]+", str(text or "").replace("‌", "")) if len(token) >= 3}


def cue_is_kept(cue: Cue, note_text: str, *, note_stems: set[str] | None = None) -> bool:
    """True when the note carries the cue: at least half of its content stems (and three of them) appear."""
    stems = _stems(cue.sentence)
    if len(stems) < 3:
        return True  # too short to judge; never report a guess
    note = note_stems if note_stems is not None else _stems(note_text)
    return len(stems & note) / len(stems) >= 0.5


def missing_cues(cues: list[Cue], note_text: str) -> list[Cue]:
    note = _stems(note_text)
    return [cue for cue in cues if not cue_is_kept(cue, note_text, note_stems=note)]
