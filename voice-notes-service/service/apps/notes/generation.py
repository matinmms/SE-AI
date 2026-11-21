from __future__ import annotations

import hashlib
import inspect
import json
import logging
import queue
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from django.conf import settings
from django.db import close_old_connections, connections
from django.utils import timezone

from apps.ai_pipeline.exceptions import PipelineError
from apps.ai_pipeline.heartbeat import HeartbeatService
from apps.ai_pipeline.jobs import ProcessingJobService
from apps.lectures.language_options import is_rtl_language, language_label, stt_language_code
from apps.lectures.models import Lecture, LectureStatus
from apps.notes.codeblock import clean_code, normalize_language, split_code_for_pages
from apps.notes.composer import NoteGenerationInputComposer
from apps.notes.enrichment import SemanticNoteEnricher
from apps.notes.foreign_terms import arabic_terms, foreign_terms_hint
from apps.notes.lecturer_cues import required_remarks_prompt
from apps.notes.reliability import damaged_chunks_prompt, damaged_excerpt_prompt
from apps.notes.models import GeneratedNote, NoteGenerationStatus
from apps.notes.protected_terms import lost_terms
from apps.notes.providers import GapGPTNoteProvider
from apps.notes.renderers import (
    SemanticNoteRenderer,
    _normalized_text_item,
    canonical_semantic_block_type,
    is_summary_semantic_block,
    semantic_labels_for_language,
)
from apps.notes.services import GeneratedNoteService
from apps.notes.themes import normalize_note_theme
from apps.storage_lifecycle.paths import temporary_artifact_path
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from infrastructure.observability.scaling import measure_scaling_stage
from infrastructure.storage.factory import get_storage_service
from services.ai.health import summarize_completion_results
from services.auth.phone import InvalidPhoneNumberError, normalize_iranian_mobile_number
from apps.notes.term_guard import (
    apply_term_guard,
    cue_stem_sets,
    demote_unbacked_exam_statements,
    term_guard_mode,
)
from apps.notes.quality_rev import (
    QUALITY_LABEL_V2,
    QUALITY_LABEL_V4,
    QUALITY_REV,
    QUALITY_REV_V42,
    QUALITY_LABEL_V22,
    QUALITY_LABEL_V42,
    clean_units_v42,
    looks_like_file_name,
    quality_rev_v42,
    should_use_quality_rev_v42,
    strip_leaked_labels,
    add_quality_rules_to_writer,
    apply_planner_shape_rules,
    apply_v2_planner_rules,
    apply_v2_writer_rules,
    time_order_topics,
    v2_length_hint_text,
    v2_part_is_short,
    V2_RETRY_GAIN,
    apply_story_rule,
    apply_v4_shape_rules,
    budget_ai_additions,
    merge_thin_units,
    section_intro_text,
    full_coverage_takeaways,
    merge_exam_statements,
    numbers_missing_from_source,
    quality_rev_on,
    quality_rev_prompt,
    scrub_unit,
    supported_numbers,
    V42_REPEAT_OVERLAP,
    drop_repeated_sentences,
    clean_unit_formulas,
    dissolve_meta_topics,
    passages_missing_from_written,
    v2_missing_passages_hint_text,
    should_use_quality_rev,
    should_use_quality_rev_v41,
)
from services.errors.recorder import ErrorRecordingService
from services.lectures.processing import LectureProcessingStateService
from services.usage.lifecycle import UsageLogService

logger = logging.getLogger("tesanote")


class _TopicWritingAborted(Exception):
    """Raised inside a topic worker once a sibling topic has already failed."""


try:
    import sqlparse
except Exception:  # pragma: no cover - fallback if formatter is unavailable
    sqlparse = None


# High-frequency function words that show up in virtually any Persian or
# English academic sentence regardless of subject matter. Slide-relevance
# scoring (_keyword_set) excludes them so that keyword overlap reflects
# genuinely shared technical vocabulary instead of incidental stopword
# matches -- without this, an entirely unrelated slide deck could still
# accumulate enough overlap to be wrongly attached as a reference.
PERSIAN_STOPWORDS: frozenset[str] = frozenset(
    {
        "است", "این", "آن", "را", "به", "از", "در", "با", "برای", "شود",
        "شد", "کرد", "کند", "باید", "بود", "بودند", "هست", "هستند", "اند",
        "ایم", "اید", "هر", "یک", "دو", "نیز", "همچنین", "یعنی", "مثلا",
        "مانند", "طور", "گونه", "بسیار", "خیلی", "بیشتر", "کمتر", "همه",
        "هیچ", "دارای", "دارد", "داشت", "داشته", "باشد", "باشند", "تا",
        "اما", "ولی", "یا", "اگر", "چون", "زیرا", "پس", "سپس", "وقتی",
        "هنگام", "نسبت", "بر", "روی", "زیر", "بالا", "جلو", "عقب", "بین",
        "میان", "طی", "ضمن", "علاوه", "بنابراین", "درنتیجه", "نتیجه",
        "صورت", "حالت", "نوع", "بخش", "قسمت", "مورد", "درباره", "رابطه",
        "نظر", "طبق", "براساس", "اساس", "کل", "تمام", "جزء", "عنوان",
        "نام", "شکل", "همان", "همین", "آنها", "ایشان", "آنان", "ما", "شما",
        "من", "تو", "او", "که", "های", "ها", "می‌شود", "می‌کند", "چند",
        "چندین", "دیگر", "دیگری", "فقط", "البته", "اینکه", "کرده",
        "کرده‌اند", "شده", "شده‌است", "داده", "کنیم", "کنید", "کنند",
        "دهد", "دهند", "گفت", "گفته",
    }
)

ENGLISH_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "are", "but", "not", "you", "all", "can",
        "her", "was", "one", "our", "out", "day", "get", "has", "him",
        "his", "how", "man", "new", "now", "old", "see", "two", "way",
        "who", "boy", "did", "its", "let", "put", "say", "she", "too",
        "use", "that", "with", "have", "this", "will", "your", "from",
        "they", "know", "want", "been", "good", "much", "some", "time",
        "very", "when", "come", "here", "just", "like", "long", "make",
        "many", "over", "such", "take", "than", "them", "well", "were",
        "what", "into", "also", "each", "more", "most", "other", "these",
        "those", "which", "while", "about", "after", "again", "being",
        "where", "there", "their", "then",
    }
)


def normalize_note_variant(model_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-")
    return normalized or "model"


def configured_note_models() -> list[str]:
    configured = [model.strip() for model in settings.NOTE_AB_TEST_MODELS if model.strip()]
    unique = list(dict.fromkeys(configured))
    return unique or [settings.NOTE_PRIMARY_MODEL]


def configured_note_fallback_models(*, primary_model: str) -> list[str]:
    configured = [model.strip() for model in settings.NOTE_FALLBACK_MODELS if model.strip()]
    filtered = [model for model in dict.fromkeys(configured) if model != primary_model]
    return filtered


def note_generation_candidate_models(*, preferred_models: list[str] | None = None) -> list[str]:
    primary_models = preferred_models or configured_note_models()
    fallbacks: list[str] = []
    for model_name in primary_models:
        fallbacks.extend(configured_note_fallback_models(primary_model=model_name))
    return [model for model in dict.fromkeys([*primary_models, *fallbacks]) if model]


def should_use_generation_pipeline_v2(*, user: Any) -> bool:
    """The structure-first pipeline is the default for every user.

    NOTE_GENERATION_V2_ENABLED is the kill switch: set it to False to send all
    lectures back through the v1 chunked pipeline without a code change.
    """
    return bool(settings.NOTE_GENERATION_V2_ENABLED) and user is not None


def should_use_generation_pipeline_v4(*, user: Any) -> bool:
    """Pedagogy-first teaching style (built on the v3 pipeline).

    NOTE_GENERATION_V4_ENABLED is the kill switch. With NOTE_GENERATION_V4_FOR_ALL
    every user gets v4; otherwise only accounts whose login phone number is in
    NOTE_GENERATION_V4_PHONE_NUMBERS do. v4 reuses the whole v3 machinery, so it
    also requires v3 to be enabled.
    """
    if not settings.NOTE_GENERATION_V4_ENABLED or not settings.NOTE_GENERATION_V3_ENABLED:
        return False
    if settings.NOTE_GENERATION_V4_FOR_ALL:
        return user is not None
    phone = str(getattr(user, "phone_number", "") or "").strip()
    if not phone:
        return False
    try:
        phone = normalize_iranian_mobile_number(phone)
    except InvalidPhoneNumberError:
        return False
    allowed: set[str] = set()
    for raw in settings.NOTE_GENERATION_V4_PHONE_NUMBERS:
        try:
            allowed.add(normalize_iranian_mobile_number(str(raw)))
        except InvalidPhoneNumberError:
            continue
    return phone in allowed


COMPARISON_V2_VARIANT_SUFFIX = "-compare-v2"


def comparison_v2_variant(model_name: str) -> str:
    return f"{normalize_note_variant(model_name)}{COMPARISON_V2_VARIANT_SUFFIX}"


def is_comparison_note_variant(variant: str) -> bool:
    """Extra notes of a lecture never decide its status or primary note.

    That is the admin-only v2 comparison note and the other styles a student asks for later
    (``<model>-alt-<style>``, see apps.notes.note_styles).
    """
    from apps.notes.note_styles import is_alt_variant

    return str(variant or "").endswith(COMPARISON_V2_VARIANT_SUFFIX) or is_alt_variant(variant)


def should_generate_comparison_v2_note(*, user: Any) -> bool:
    """Staff accounts listed in NOTE_COMPARISON_V2_PHONE_NUMBERS also get a v2 note per lecture."""
    if not settings.NOTE_COMPARISON_V2_ENABLED or user is None:
        return False
    if not getattr(user, "is_staff", False):
        return False
    phone = str(getattr(user, "phone_number", "") or "").strip()
    if not phone:
        return False
    try:
        phone = normalize_iranian_mobile_number(phone)
    except InvalidPhoneNumberError:
        return False
    allowed: set[str] = set()
    for raw in settings.NOTE_COMPARISON_V2_PHONE_NUMBERS:
        try:
            allowed.add(normalize_iranian_mobile_number(str(raw)))
        except InvalidPhoneNumberError:
            continue
    return phone in allowed


def should_use_generation_pipeline_v3(*, user: Any) -> bool:
    """A/B gate for the professor-rewrite pipeline, keyed by user id.

    Id (not phone/email) is the one identifier that survives a person having
    several accounts. NOTE_GENERATION_V3_FOR_ALL sends every user through v3;
    set it to False to return to the allowlist. The v2/v1 pipelines stay
    untouched and are used whenever v3 is off for a user.
    """
    if not settings.NOTE_GENERATION_V3_ENABLED:
        return False
    if should_use_generation_pipeline_v4(user=user):
        return True
    if settings.NOTE_GENERATION_V3_FOR_ALL:
        return user is not None
    user_id = str(getattr(user, "id", "") or "").strip()
    if not user_id:
        return False
    return user_id in {str(allowed).strip() for allowed in settings.NOTE_GENERATION_V3_USER_IDS}


def selected_note_models_for_lecture(*, lecture: Lecture) -> list[str]:
    if should_use_generation_pipeline_v3(user=lecture.user):
        return [settings.NOTE_GENERATION_V3_MODEL]
    if should_use_generation_pipeline_v2(user=lecture.user):
        return [settings.NOTE_GENERATION_V2_MODEL]
    processing_metadata = lecture.processing_metadata or {}
    selected_model = str(processing_metadata.get("note_generation_selected_model", "") or "").strip()
    if selected_model:
        return [selected_model]
    return configured_note_models()


def selected_note_fallback_models_for_lecture(*, lecture: Lecture, primary_model: str) -> list[str]:
    processing_metadata = lecture.processing_metadata or {}
    selected_fallbacks = [
        str(model).strip()
        for model in processing_metadata.get("note_generation_fallback_models", []) or []
        if str(model).strip()
    ]
    if selected_fallbacks:
        return [model for model in dict.fromkeys(selected_fallbacks) if model != primary_model]
    return configured_note_fallback_models(primary_model=primary_model)


TESANOTE_ADDITIONS_PROMPT = (
            "You may go beyond the lecturer in exactly six ways, each kept short and each "
            "standard textbook knowledge of this subject: (1) one or two sentences of "
            "prerequisite background when the lecturer used a term before defining it, (2) "
            "completing an example the lecturer left ambiguous or pointed at on an unseen "
            "board, (3) correcting something the lecturer appears to have misspoken, in one "
            "brief sentence, (4) a plain-words intuition or the 'why' for a concept the "
            "lecturer only stated, (5) the conventional meaning of a symbol or unit, or the "
            "common mistake students make with that exact point, (6) in a programming or "
            "algorithms course, when the lecturer explains a procedure or algorithm only in "
            "words, a SHORT code_blocks entry (the language of the course, or pseudocode) that "
            "does exactly what was described and nothing more -- this is the one addition that "
            "goes into code_blocks, and its ai_added line says only that the code was written "
            "from the lecturer's description. Code the lecturer actually showed or dictated is "
            "copied as spoken and needs no such line. Do not add new topics, "
            "theorems, extra worked examples or content that goes past what the lecturer and "
            "slides covered, and never contradict the lecturer. "
    " EVERYTHING you add under these six ways -- and every example you reconstruct or "
    "solve yourself because the lecturer did not give it -- is TesaNote's own, not the "
    "lecturer's, and the student must be able to tell. Never mix it into body, list_items "
    "or note: put each addition, as its own short self-contained piece of text "
    "(one or two sentences), into the unit's ai_added array, and for a reconstructed or "
    "self-solved example set example_ai to true. Whatever the lecturer actually said stays "
    "in body, list_items, note and example as usual. When a passage is too broken to restore "
    "with certainty, still write the best-supported version of the point (never omit it) and "
    "put the part you had to infer into ai_added. "
    "WRITE EVERY ai_added PIECE FOR A STUDENT WHO FINDS THE SUBJECT HARD: use everyday, "
    "simple words and very short sentences (one idea each, about fifteen words at most); "
    "no jargon, no abbreviations and no formal textbook phrasing; if a technical term is "
    "unavoidable, say what it means in plain words right away, and give a tiny everyday "
    "example when that makes it clearer. Say what it means for the student (what to "
    "remember, why it matters, what to watch out for) instead of repeating the "
    "lecturer's definition in fancier words. "
    "SCIENTIFIC ACCURACY WHILE YOU WRITE: the transcript is machine-made, so a mis-heard word can "
    "turn a true statement into a false one. Check every statement you write against standard "
    "knowledge of the subject. A word you can restore with certainty from the context or the "
    "slides is simply written correctly. When a statement is scientifically wrong (a wrong "
    "term, number, formula, complexity, sign, name or definition), never copy it: write the "
    "correct version in its place, inside the same sentence of body, list_items, note or "
    "example, wrapped as [[added:the correct text]] -- the note shows it with the label "
    "\"added by TesaNote\" so the student can tell it was corrected. Use it only for a real "
    "error you are certain about, keep it to the shortest phrase that fixes the claim, and "
    "never for style, missing detail or the lecturer's legitimate simplification. "
)


SIMPLE_TONE_PROMPT = (
    "TONE: write like a friendly, clear tutor explaining to a classmate, not like an official "
    "textbook or a report. Use simple, everyday words of the output language and short, "
    "direct sentences; prefer active verbs and a direct 'you' / imperative (\"first do X, "
    "then Y\") over bookish passive phrasing, legal-sounding wording and filler such as "
    "\"it should be noted that\", \"it is necessary to\" or \"is carried out\". Where a "
    "plain word exists, use it instead of the formal one. Keep technical terms (with their "
    "meaning) and keep it respectful: no slang and no jokes. "
)


ONE_HOME_PROMPT = (
    "ONE HOME FOR EVERY IDEA: each fact appears in exactly ONE field of ONE unit; the same "
    "idea written as a paragraph, again as a list and again as an example is the worst "
    "flaw a note can have. Decide where each idea lives: a sequence of steps or an "
    "enumeration goes in list_items, and then body holds only a single lead-in sentence "
    "that does NOT repeat the steps; an explanation without steps goes in body and has no "
    "list; note holds only a caution or exam hint that body and list do not already say; "
    "example is used only for a concrete case with its own specific values or scenario, "
    "and is left empty when it would just walk through the procedure already given. "
    "Before writing a sentence, check it adds something the unit has not said yet. "
)


LECTURER_WORDS_PROMPT = (
    "THE LECTURER'S OWN WORDS: this note is the lecturer's lecture written down, so the student "
    "reads what the lecturer actually said. Keep the lecturer's own sentences, word choices, "
    "terms, analogies, examples and emphasis, as close to verbatim as written text allows. Edit "
    "a spoken sentence only as much as it takes to read as a correct written sentence of the "
    "output language: drop fillers and false starts, finish a sentence the lecturer clearly "
    "finished in meaning, fix grammar the speech broke, and correct speech-to-text errors. Do "
    "NOT paraphrase into your own words, do NOT turn the lecturer's explanation into a "
    "textbook or tutor voice, do NOT simplify or formalise their wording, and do NOT summarise "
    "a passage the lecturer explained in detail -- if they explained a point in four "
    "sentences, keep those four sentences (minus pure repetition). Write it as the lecturer "
    "speaking the material, not as narration about them (never \"the professor says\"). "
)


FAITHFUL_NOTE_PROMPT = (
    "FAITHFUL NOTE: this note must read as the professor's own lecture, tidied -- not as a "
    "re-teaching. Keep the professor's order, his explanations, his examples and his emphasis; "
    "only clean the speech into well-formed written sentences. Add nothing he did not say: no "
    "extra background, definitions, examples, intuition or corrections of your own, and no "
    "'the professor said' narration. Remove only spoken noise: filler, restarted sentences, "
    "greetings, repetition of a point already made. REPEATED TALK: lecturers repeat and "
    "rephrase themselves, so state each point ONCE, in its first and clearest form, and "
    "leave out later restatements, 'as I said', recaps of an earlier topic and the same "
    "idea re-explained in other words; keep a later mention only when it adds something "
    "new (a number, a condition, an exception, a new example, an exam hint). Never repeat "
    "a point across body, note, example and list_items, or across units. Everything the "
    "professor taught that bears on the course or the exam stays. UNCLEAR OR BROKEN PASSAGES: when the "
    "transcript is garbled, cut off or incomplete, never drop the point and never leave it "
    "half-written; restore what the professor evidently meant from the surrounding speech, "
    "the slides and the rest of the lecture, and write it as a complete, understandable "
    "statement of that same point. Separate the kinds of content cleanly: "
    "the explanation in body; a step sequence or enumeration in list_items; every worked "
    "example, with its setup and result, in example (never inside body); every caution, tip, "
    "exam hint or remark the professor stressed in note (never inside body); key equations in "
    "formulas. "
)


TERM_PRESERVATION_PROMPT = (
    "FOREIGN-TERM RULE (it covers every field you write: concept_title, body, list_items, "
    "tables, note and example). Whenever the lecturer says a word, term, abbreviation or "
    "name in English (or any language other than the output language) while teaching, the "
    "note must contain it in its original Latin spelling and correct casing (e.g. pipeline, "
    "overfitting, Monolithic, CPU). The transcript is speech-to-text and often writes such "
    "words phonetically in Persian script (e.g. «اورفیتینگ», «پایپ‌لاین», «کامپایلر»), "
    "sometimes beside a Persian equivalent the lecturer also said: recognise them and "
    "restore the real English spelling. When you word the idea with the Persian equivalent, "
    "put the English term in parentheses right after it on its first use in the unit, and in "
    "concept_title when the unit is about that term, e.g. «بیش‌برازش (overfitting)»; when the "
    "lecturer used the English term itself as the working term, write it in Latin inside the "
    "sentence. Never translate it away and never drop it, including when you shorten or "
    "rewrite text. Never add English terms the lecturer did not say, and never put English "
    "next to ordinary Persian vocabulary. "
)

_LATIN_TERM_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9+#]*(?:[-/.'][A-Za-z0-9+#]+)*"
    r"(?: [A-Za-z][A-Za-z0-9+#]*(?:[-/.'][A-Za-z0-9+#]+)*){0,3}(?![A-Za-z0-9_])"
)
_MAX_SPOKEN_ENGLISH_TERMS = 40


def spoken_english_terms(text: str, *, limit: int = _MAX_SPOKEN_ENGLISH_TERMS) -> list[str]:
    """English words and phrases the transcript spells in Latin letters, first-seen order."""
    seen: set[str] = set()
    terms: list[str] = []
    for match in _LATIN_TERM_RE.finditer(str(text or "")):
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


SPEECH_TO_TEXT_CORRECTION_PROMPT = (
    "SPECIALIZED-TERM CORRECTION: the transcript is machine speech-to-text, so technical "
    "terms, names and abbreviations are often misspelled, split, or replaced by a "
    "similar-sounding ordinary word or Persian-script transliteration. Decide each such "
    "word from the WHOLE lecture, not the sentence alone: use the lecture title, course "
    "hints, slides, the lecture-wide term list and every other place the same term appears "
    "(a consistent, correctly spelled occurrence elsewhere wins over a garbled one). Write "
    "the single correct form of the term, spelled the same way everywhere in the note, "
    "restoring the real English spelling when the lecturer said it in English. Correct only "
    "when the context makes the intended term clear; otherwise keep the transcript wording "
    "and never invent a term. "
)


FORMULA_FIELD_PROMPT = (
    "- formulas: an array of strings, one per key equation, law or calculation formula "
    "the student must know or apply (engineering, physics, math, statistics, finance and "
    "similar courses have many); an empty array when there is none. Write each as compact "
    "LaTeX-lite WITHOUT dollar signs or \\( \\): \\frac{a}{b}, x^2, x_{1}, x_{max}, \\sqrt{x}, \\sum, "
    "\\int, \\alpha, \\times, \\cdot, \\leq, \\geq, \\approx, using the real symbols and "
    "numbers of the lecture. Vectors and matrices use \\begin{bmatrix} x \\\\ y \\\\ \\theta "
    "\\end{bmatrix} (\\\\ between rows, & between columns; also pmatrix, cases, aligned), "
    "accents use braces: \\dot{x}, \\hat{x}, \\vec{v}; words inside a formula use "
    "\\text{...}; degrees are 30^\\circ. Always write the braces after \\begin, \\end and "
    "every command argument (\\begin{bmatrix}, \\dot{x}, never \\beginbmatrix or \\dotx). "
    "Put a formula here INSTEAD of burying it in the body; the "
    "body then says in words what it means and when to use it, without repeating it. "
    "Subscripts and superscripts carry only their own index: write C_{3} n^{2} (never "
    "C_{3n}^{2}) for 'C sub 3 times n squared', and give every term of a sum its own braces, "
    "e.g. T_{n}^{(h)} = C_{1} + C_{2} n + C_{3} n^{2} + C_{4} 5^{n}. "
    "Never write an equation inside body, example or note text (such as y_pred = yi or "
    "sqrt((x_A1 - x_B1)^2)); refer to it in words and put it in formulas\n"
)


CODE_FIELD_PROMPT = (
    "- code_blocks: an array of objects {\"language\", \"code\", \"caption\"}, one per piece of "
    "source code the professor actually writes, shows, dictates or walks through (programming, "
    "SQL, shell, MATLAB, HDL, assembly or pseudocode in engineering courses); an empty array "
    "when the unit has no code. language is the lowercase language name (python, c, cpp, java, "
    "javascript, sql, bash, matlab, verilog, vhdl, assembly, pseudocode...); code is the code "
    "itself, with real line breaks and indentation, in its original language and never "
    "translated; caption is one short phrase in the note language saying what the snippet does "
    "(may be empty). Reconstruct the code only from what the lecturer or the slides actually "
    "show; never invent code, unless the system instructions explicitly allow a short code "
    "block for a procedure explained only in words. Do not repeat the code in body, example or note: say in words "
    "what it does and why. Plain commands or identifiers mentioned in passing stay in the "
    "body text\n"
)
FORMULA_FIELD_PROMPT = FORMULA_FIELD_PROMPT + CODE_FIELD_PROMPT


TABLE_FIELD_PROMPT = (
    "- tables: an array of objects {\"caption\", \"headers\", \"rows\"}, one per real table. "
    "Create a table whenever the material is naturally tabular: a comparison of two or more "
    "things across the same attributes, a classification, a list of terms with several "
    "properties each, a truth table, a data or parameter table, or a table shown on a slide. "
    "headers is an array of short column titles; rows is an array of rows, each an array of "
    "plain strings with exactly as many cells as headers. caption is one short phrase in the "
    "note language (may be empty). Fill cells only from what the lecturer or slides actually "
    "state; never invent values, and write \"-\" for a cell the source leaves empty. Do not "
    "repeat the table's content in body, list_items, note or example: say in words what it "
    "shows. Use an empty array when the unit has no table\n"
)
FORMULA_FIELD_PROMPT = FORMULA_FIELD_PROMPT + TABLE_FIELD_PROMPT

EXAM_FIELD_PROMPT = (
    "- When the professor says something about the exam for this material (it is or is not "
    "on the exam, is important for it, will be asked, can be skipped), keep it as its own "
    "short faithful sentence inside body (or note) and begin that sentence with the marker "
    "[[exam:important]] (material that matters for the exam) or [[exam:excluded]] (material "
    "that will not be on the exam). The marker goes right before the sentence, one marker "
    "per sentence, written in the output language without the marker inside the sentence. "
    "Never invent exam statements the professor did not make\n"
)
FORMULA_FIELD_PROMPT = FORMULA_FIELD_PROMPT + EXAM_FIELD_PROMPT

_EXAM_MARKER = re.compile(r"\[\[exam:(important|excluded)\]\]\s*", re.IGNORECASE)
_EXAM_WORDS = re.compile(
    r"امتحان|آزمون|ازمون|میان[\u200c ]?ترم|پایان[\u200c ]?ترم|کوئیز|کوییز|\bexam\b|\bmidterm\b|\bfinal\b|\bquiz\b",
    re.IGNORECASE,
)
_EXAM_EXCLUDED_CUES = re.compile(
    r"نمی[\u200c ]?(?:آ|ا)(?:ید|د|یاد)|نمیاد|نمیاید|نیاد|نمی[\u200c ]?(?:پرس|دهم|دم)|خارج از|نیست|حذف|"
    r"not (?:be )?(?:on|in|covered|asked)|won'?t|skip",
    re.IGNORECASE,
)
_EXAM_IMPORTANT_CUES = re.compile(
    r"مهم|می[\u200c ]?(?:آ|ا)(?:ید|د|یاد)|میاد|میاید|حتما|حتماً|می[\u200c ]?پرس|سوال|سؤال|"
    r"important|will be (?:on|asked)|asked|must",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟۔])\s+")
# Quality revision: «آزمون» is also the test SET in ML/statistics lectures and «احتمال» contains
# «حتما», so the loose net turned "the test sentences' probability" into an exam box. The strict
# net only trusts unambiguous exam words and whole-word cues.
_FA_EDGE = r"(?<![\u0600-\u06FF\u200c])"
_FA_END = r"(?![\u0600-\u06FF])"
_STRICT_EXAM_WORDS = re.compile(
    r"امتحان|میان[\u200c ]?ترم|پایان[\u200c ]?ترم|کوئیز|کوییز|\bexam\b|\bmidterm\b|\bquiz\b",
    re.IGNORECASE,
)
_STRICT_IMPORTANT_CUES = re.compile(
    rf"{_FA_EDGE}(?:مهم|حتما|حتماً|سوال|سؤال){_FA_END}|می[\u200c ]?پرس|"
    r"می[\u200c ]?خوا(?:هم|م)\b|میخوام|می[\u200c ]?(?:آید|آد)\b|میاد\b|می[\u200c ]?دهم\b|میدم\b|"
    r"\bimportant\b|will be (?:on|asked)|\basked\b",
    re.IGNORECASE,
)


# Revision 3: «در امتحان می‌توانید ...» is an exam remark even without a «مهم» cue; «امتحان کنید» (try it) is not.
_BROAD_EXAM_USE = re.compile(r"امتحان\s*(?:کن|کرد|می[\u200c ]?کن|شد|می[\u200c ]?شد)")
_QUIZ_WORDS = re.compile(r"کو[ئی]{1,2}ز|\bquiz\b", re.IGNORECASE)


def extract_exam_statements(
    text: str, *, strict: bool = False, broad: bool = False
) -> tuple[str, list[tuple[str, str]]]:
    """Pull the professor's exam remarks out of prose: (clean_text, [(status, sentence)]).

    Honors the model's [[exam:...]] markers and, as a safety net, recognizes unmarked
    sentences that mention the exam together with a clear importance / exclusion cue.
    """
    source = str(text or "")
    exam_words = _STRICT_EXAM_WORDS if strict else _EXAM_WORDS
    if broad:
        exam_words = re.compile(f"{exam_words.pattern}|{_QUIZ_WORDS.pattern}", re.IGNORECASE)
    important_cues = _STRICT_IMPORTANT_CUES if strict else _EXAM_IMPORTANT_CUES
    if not source.strip() or not (_EXAM_MARKER.search(source) or exam_words.search(source)):
        return source, []
    kept: list[str] = []
    found: list[tuple[str, str]] = []
    for paragraph in re.split(r"(\n\s*\n)", source):
        if not paragraph.strip():
            kept.append(paragraph)
            continue
        remaining: list[str] = []
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            marker = _EXAM_MARKER.search(sentence)
            if marker:
                status = marker.group(1).lower()
                clean = _EXAM_MARKER.sub("", sentence).strip()
            elif exam_words.search(sentence) and len(sentence) <= 320:
                if _EXAM_EXCLUDED_CUES.search(sentence):
                    status = "excluded"
                elif important_cues.search(sentence) or (
                    broad and not _BROAD_EXAM_USE.search(sentence)
                ):
                    status = "important"
                else:
                    remaining.append(sentence)
                    continue
                clean = sentence.strip()
            else:
                remaining.append(sentence)
                continue
            if clean:
                found.append((status, clean))
        kept.append(" ".join(part.strip() for part in remaining if part.strip()))
    clean_text = "".join(kept)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()
    return clean_text, found


@dataclass(frozen=True)
class NoteModelProbeResult:
    model_name: str
    available: bool
    provider_used: str = ""
    model_used: str = ""
    fallback_used: bool = False
    error_message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "model_name": self.model_name,
            "available": self.available,
            "provider_used": self.provider_used,
            "model_used": self.model_used,
            "fallback_used": self.fallback_used,
            "error_message": self.error_message,
        }


_NOTE_MODEL_PROBE_CACHE: dict[str, tuple[float, NoteModelProbeResult]] = {}


class NoteGenerationReadinessService:
    probe_cache_ttl_seconds = 300.0

    def __init__(self, *, provider_factory: Any | None = None) -> None:
        self.provider_factory = provider_factory or (lambda model_name: GapGPTNoteProvider(model_name=model_name))

    def select_working_model(
        self,
        *,
        preferred_models: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        candidates = note_generation_candidate_models(preferred_models=preferred_models)
        probe_results = [
            self.probe_model(model_name=model_name, force_refresh=force_refresh)
            for model_name in candidates
        ]
        healthy_models = [result.model_name for result in probe_results if result.available]
        if not healthy_models:
            details = " | ".join(
                f"{result.model_name}: {result.error_message or 'unavailable'}"
                for result in probe_results
            ) or "No note-generation model responded successfully."
            raise PipelineError(
                stage="NOTE_MODEL_PRECHECK",
                message=(
                    "No note-generation model is available right now. "
                    f"Please try again later. {details}"
                ),
            )

        return {
            "selected_model": healthy_models[0],
            "fallback_models": healthy_models[1:],
            "probes": [result.as_dict() for result in probe_results],
        }

    def persist_selection(
        self,
        *,
        lecture: Lecture,
        preferred_models: list[str] | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        selection = self.select_working_model(
            preferred_models=preferred_models,
            force_refresh=force_refresh,
        )
        lecture.refresh_from_db()
        processing_metadata = dict(lecture.processing_metadata or {})
        processing_metadata["note_generation_selected_model"] = selection["selected_model"]
        processing_metadata["note_generation_fallback_models"] = selection["fallback_models"]
        processing_metadata["note_generation_model_probe_checked_at"] = timezone.now().isoformat()
        processing_metadata["note_generation_model_probe_results"] = selection["probes"]
        lecture.processing_metadata = processing_metadata
        lecture.save(update_fields=["processing_metadata", "updated_at"])
        return selection

    def probe_model(self, *, model_name: str, force_refresh: bool = False) -> NoteModelProbeResult:
        cache_key = model_name.strip()
        cached = _NOTE_MODEL_PROBE_CACHE.get(cache_key)
        now_monotonic = time.monotonic()
        if (
            not force_refresh
            and cached is not None
            and now_monotonic - cached[0] < self.probe_cache_ttl_seconds
        ):
            return cached[1]

        system_prompt = "You are a health-check responder for an academic note generator. Return JSON only."
        user_prompt = (
            "Return a minimal semantic note JSON object with keys document_title and pages. "
            "pages must contain one page object with a blocks array containing one paragraph block. "
            'Use this exact structure: {"document_title":"آماده","pages":[{"header":{},"blocks":[{"type":"paragraph","content":"مدل آماده است."}],"footer":{}}]}'
        )
        try:
            provider = self.provider_factory(model_name)
            payload, result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            self._validate_probe_payload(payload=payload, model_name=model_name)
            probe_result = NoteModelProbeResult(
                model_name=model_name,
                available=True,
                provider_used=str(result.provider or ""),
                model_used=str(result.model or model_name),
                fallback_used=bool(result.fallback_used),
            )
        except PipelineError as exc:
            probe_result = NoteModelProbeResult(
                model_name=model_name,
                available=False,
                error_message=exc.message,
            )
        except Exception as exc:  # pragma: no cover - defensive guard for unexpected provider failures
            probe_result = NoteModelProbeResult(
                model_name=model_name,
                available=False,
                error_message=str(exc) or "Unexpected note-model probe failure.",
            )

        if probe_result.available:
            _NOTE_MODEL_PROBE_CACHE[cache_key] = (now_monotonic, probe_result)
        else:
            _NOTE_MODEL_PROBE_CACHE.pop(cache_key, None)
        return probe_result

    @staticmethod
    def _validate_probe_payload(*, payload: dict[str, Any], model_name: str) -> None:
        if not isinstance(payload, dict):
            raise PipelineError(
                stage="NOTE_MODEL_PRECHECK",
                message=f"{model_name} returned a non-object probe payload.",
            )
        pages = payload.get("pages")
        if not isinstance(pages, list) or not pages:
            raise PipelineError(
                stage="NOTE_MODEL_PRECHECK",
                message=f"{model_name} returned a probe payload without pages.",
            )
        first_page = pages[0]
        if not isinstance(first_page, dict):
            raise PipelineError(
                stage="NOTE_MODEL_PRECHECK",
                message=f"{model_name} returned an invalid probe page.",
            )
        blocks = first_page.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            raise PipelineError(
                stage="NOTE_MODEL_PRECHECK",
                message=f"{model_name} returned a probe payload without blocks.",
            )


def aggregate_note_status(*, lecture: Lecture) -> tuple[str, int]:
    notes = [note for note in lecture.generated_notes.all() if not is_comparison_note_variant(note.variant)]
    if not notes:
        return NoteGenerationStatus.NOT_STARTED, 0

    statuses = {note.status for note in notes}
    if any(status == NoteGenerationStatus.FAILED for status in statuses):
        return NoteGenerationStatus.FAILED, 100
    if all(status == NoteGenerationStatus.COMPLETED for status in statuses):
        return NoteGenerationStatus.COMPLETED, 100
    if any(status == NoteGenerationStatus.RENDERING for status in statuses):
        return NoteGenerationStatus.RENDERING, min(99, max(note.progress for note in notes))
    if any(status == NoteGenerationStatus.STRUCTURING for status in statuses):
        return NoteGenerationStatus.STRUCTURING, max(note.progress for note in notes)
    if any(status == NoteGenerationStatus.QUEUED for status in statuses):
        return NoteGenerationStatus.QUEUED, max(note.progress for note in notes)
    return NoteGenerationStatus.NOT_STARTED, max(note.progress for note in notes)


COMPARISON_TITLE_LABELS = {"v4": " — نسخهٔ v4", "v2": " — نسخهٔ v2 (عین گفته‌های استاد)"}
QUALITY_TITLE_LABELS = {
    "v4": f" — نسخهٔ {QUALITY_LABEL_V4}",
    "v2": f" — نسخهٔ {QUALITY_LABEL_V2} (عین گفته‌های استاد)",
}
QUALITY_V42_TITLE_LABELS = {
    "v4": f" — نسخهٔ {QUALITY_LABEL_V42}",
    "v2": f" — نسخهٔ {QUALITY_LABEL_V22} (عین گفته‌های استاد)",
}


def label_comparison_titles(*, lecture: Lecture, comparison_note: GeneratedNote) -> None:
    """Name the two notes so the admin can tell them apart in the note list."""
    if should_use_quality_rev_v42(user=lecture.user):
        labels = QUALITY_V42_TITLE_LABELS
    elif should_use_quality_rev(user=lecture.user):
        labels = QUALITY_TITLE_LABELS
    else:
        labels = COMPARISON_TITLE_LABELS
    for note in lecture.generated_notes.filter(status=NoteGenerationStatus.COMPLETED):
        label = labels["v2" if note.id == comparison_note.id else "v4"]
        if is_comparison_note_variant(note.variant) and note.id != comparison_note.id:
            continue
        title = str(note.title or lecture.title or "").strip()
        if title.endswith(label):
            continue
        for other in (*COMPARISON_TITLE_LABELS.values(), *QUALITY_TITLE_LABELS.values(), *QUALITY_V42_TITLE_LABELS.values()):
            if title.endswith(other):
                title = title[: -len(other)]
        note.title = f"{title}{label}"[:255]
        note.save(update_fields=["title", "updated_at"])


def _lecture_timeout_locked(lecture: Lecture) -> bool:
    return lecture.status == LectureStatus.FAILED and bool((lecture.processing_metadata or {}).get("pipeline_timeout_reason"))


class NoteGenerationDebugService:
    @staticmethod
    def _serialize_note(note: GeneratedNote, *, include_content: bool = True) -> dict[str, Any]:
        # Status endpoints must remain read-only. Generating artifacts here caused
        # every browser poll to launch Chromium again after a failed PDF render.
        html_path = note.html_path
        pdf_path = note.pdf_path
        resolved_theme = normalize_note_theme(note.lecture.processing_metadata.get("note_theme"))
        return {
            "note_id": str(note.id) if getattr(note, "id", None) else "",
            "variant": note.variant,
            "model_name": note.model_name,
            "status": note.status,
            "progress": note.progress,
            "stage_detail": note.generation_metadata.get("current_stage_detail", ""),
            "stage_message": note.generation_metadata.get("current_stage_message", ""),
            "title": note.title,
            "summary_text": note.summary_text,
            "semantic_payload": note.semantic_payload if include_content else {},
            "html_path": html_path,
            "absolute_html_path": "",
            "pdf_path": pdf_path,
            "absolute_pdf_path": "",
            "body_html": note.body_html if include_content else "",
            "error_message": note.error_message,
            "resolved_theme": resolved_theme,
            "generation_metadata": note.generation_metadata,
            "prompt_metadata": note.prompt_metadata,
            "source_snapshot": note.source_snapshot if include_content else {},
        }

    @staticmethod
    def debug_payload(*, lecture: Lecture, include_content: bool = True) -> dict[str, Any]:
        notes = list(lecture.generated_notes.all().order_by("variant"))
        status, progress = aggregate_note_status(lecture=lecture)
        primary_variant = normalize_note_variant(settings.NOTE_PRIMARY_MODEL)
        main_notes = [note for note in notes if not is_comparison_note_variant(note.variant)]
        primary_note = next(
            (note for note in main_notes if note.variant == primary_variant), main_notes[0] if main_notes else None
        )

        if primary_note is None:
            return {
                "lecture_id": str(lecture.id),
                "note_id": "",
                "status": NoteGenerationStatus.NOT_STARTED,
                "progress": 0,
                "primary_variant": primary_variant,
                "stage_detail": "",
                "stage_message": "",
                "title": "",
                "summary_text": "",
                "semantic_payload": {},
                "html_path": "",
                "absolute_html_path": "",
                "pdf_path": "",
                "absolute_pdf_path": "",
                "body_html": "",
                "error_message": "",
                "generation_metadata": {},
                "prompt_metadata": {},
                "source_snapshot": {},
                "results": [],
            }

        primary_html_path = primary_note.html_path
        primary_pdf_path = primary_note.pdf_path
        resolved_theme = normalize_note_theme(lecture.processing_metadata.get("note_theme"))
        return {
            "lecture_id": str(lecture.id),
            "note_id": str(primary_note.id),
            "status": status,
            "progress": progress,
            "primary_variant": primary_note.variant,
            "stage_detail": primary_note.generation_metadata.get("current_stage_detail", ""),
            "stage_message": primary_note.generation_metadata.get("current_stage_message", ""),
            "title": primary_note.title,
            "summary_text": primary_note.summary_text,
            "semantic_payload": primary_note.semantic_payload if include_content else {},
            "html_path": primary_html_path,
            "absolute_html_path": "",
            "pdf_path": primary_pdf_path,
            "absolute_pdf_path": "",
            "body_html": primary_note.body_html if include_content else "",
            "error_message": primary_note.error_message,
            "resolved_theme": resolved_theme,
            "generation_metadata": primary_note.generation_metadata,
            "prompt_metadata": primary_note.prompt_metadata,
            "source_snapshot": primary_note.source_snapshot if include_content else {},
            "results": [
                NoteGenerationDebugService._serialize_note(note, include_content=include_content)
                for note in notes
            ],
        }


class NoteGenerationOrchestrator:
    example_cue_pattern = re.compile(
        r"(?:مثال|مثلاً|مثلا|برای مثال|فرض کنید|روی تخته|پای تخته|می.?نویسیم|حل می.?کنیم|"
        r"example|for example|suppose|whiteboard|on the board|write down|worked example)",
        re.IGNORECASE,
    )
    visual_example_pattern = re.compile(
        r"(?:روی (?:این )?تخته|پای تخته|اینجا (?:می.?نویسیم|داریم|قرار می.?دهیم)|"
        r"همین(?:جا)?|این (?:عدد|عبارت|فرمول|شکل)|به این صورت|"
        r"on the (?:white)?board|as written here|this (?:number|formula|expression))",
        re.IGNORECASE,
    )
    example_resolution_pattern = re.compile(
        r"(?:جواب|پاسخ|نتیجه|بنابراین|در نتیجه|پس |گام|مرحله|جایگذاری|محاسبه|باید|می.?شود|خواهد شد|"
        r"answer|result|therefore|solution|step|substitut|calculat|=)",
        re.IGNORECASE,
    )
    block_type_aliases = {
        "header": "section-header",
        "subheading": "subsection-header",
        "sub-header": "subsection-header",
        "subsection": "subsection-header",
        "example": "example-box",
        "off-topic": "unrelated-discussion-box",
        "unrelated-discussion": "unrelated-discussion-box",
        "note": "note-box",
        "highlight": "key-highlight",
        "summary": "summary-box",
        "formula": "formula-box",
        "code": "code-block",
        "figure": "figure-reference",
        "bullet-list": "list",
        "unordered-list": "list",
        "ordered-list": "list",
        "list-block": "list",
    }

    def __init__(self, *, models: list[str] | None = None, provider_factory: Any | None = None) -> None:
        self.models = models or configured_note_models()
        self.composer = NoteGenerationInputComposer()
        self.enricher = SemanticNoteEnricher()
        self.provider_factory = provider_factory or (lambda model_name: GapGPTNoteProvider(model_name=model_name))
        self.supported_block_types = set(SemanticNoteRenderer.supported_block_types())

    def get_or_create_note(self, *, lecture: Lecture, model_name: str, variant: str | None = None) -> GeneratedNote:
        try:
            variant = variant or normalize_note_variant(model_name)
            note, _ = GeneratedNote.objects.get_or_create(
                lecture=lecture,
                variant=variant,
                defaults={
                    "model_name": model_name,
                    "user": lecture.user,
                    "theme": normalize_note_theme(lecture.processing_metadata.get("note_theme")),
                },
            )
            if note.model_name != model_name:
                note.model_name = model_name
            note.user = lecture.user
            note.theme = normalize_note_theme(lecture.processing_metadata.get("note_theme"))
            note.save(update_fields=["model_name", "user", "theme", "updated_at"])
            return note
        finally:
            close_old_connections()

    @staticmethod
    def _touch_note_activity(
        *,
        lecture: Lecture,
        note: GeneratedNote | None = None,
        stage_detail: str = "",
        stage_message: str = "",
    ) -> None:
        try:
            now_iso = timezone.now().isoformat()
            processing_metadata = dict(lecture.processing_metadata or {})
            processing_metadata["note_generation_last_activity_at"] = now_iso
            processing_metadata.setdefault("note_generation_started_at", now_iso)
            if stage_detail:
                processing_metadata["note_generation_stage"] = stage_detail
            if stage_message:
                processing_metadata["note_generation_stage_message"] = stage_message
            lecture.processing_metadata = processing_metadata
            lecture.save(update_fields=["processing_metadata", "updated_at"])

            if note is not None:
                generation_metadata = dict(note.generation_metadata or {})
                generation_metadata["last_activity_at"] = now_iso
                note.generation_metadata = generation_metadata
                note.save(update_fields=["generation_metadata", "updated_at"])
        finally:
            close_old_connections()

    @staticmethod
    def _assert_lecture_not_locked(*, lecture_id: str) -> None:
        lecture = Lecture.objects.get(id=lecture_id)
        if _lecture_timeout_locked(lecture):
            raise PipelineError(
                stage="NOTE_GENERATION_TIMEOUT",
                message=lecture.processing_error or "Note generation timed out and was cancelled automatically.",
            )

    def start(
        self, *, lecture: Lecture, model_name: str, variant: str | None = None, mark_lecture: bool = True
    ) -> GeneratedNote:
        note = self.get_or_create_note(lecture=lecture, model_name=model_name, variant=variant)
        lecture.refresh_from_db()
        # The admin comparison note runs after the lecture is already finished and
        # must not move it back to "generating".
        if mark_lecture and not _lecture_timeout_locked(lecture):
            LectureProcessingStateService.mark_generating_notes(
                lecture=lecture,
                progress=max(lecture.processing_progress, 1),
            )
        note.status = NoteGenerationStatus.QUEUED
        note.progress = 0
        note.title = ""
        note.summary_text = ""
        note.semantic_payload = {}
        note.body_html = ""
        note.html_path = ""
        note.pdf_path = ""
        note.error_message = ""
        note.generation_metadata = {
            "current_stage_detail": "QUEUED",
            "current_stage_message": "Waiting to start note generation.",
            "stage_trace": [
                {
                    "status": NoteGenerationStatus.QUEUED,
                    "progress": 0,
                    "stage_detail": "QUEUED",
                    "message": "Waiting to start note generation.",
                    "timestamp": timezone.now().isoformat(),
                }
            ],
        }
        note.prompt_metadata = {}
        note.source_snapshot = {}
        note.save(
            update_fields=[
                "status",
                "progress",
                "title",
                "summary_text",
                "semantic_payload",
                "body_html",
                "html_path",
                "pdf_path",
                "error_message",
                "generation_metadata",
                "prompt_metadata",
                "source_snapshot",
                "updated_at",
            ]
        )
        self._touch_note_activity(
            lecture=lecture,
            note=note,
            stage_detail="QUEUED",
            stage_message="Waiting to start note generation.",
        )
        return note

    def update(
        self,
        *,
        note: GeneratedNote,
        status: str,
        progress: int,
        stage_detail: str | None = None,
        stage_message: str | None = None,
        **extra: Any,
    ) -> None:
        lecture = Lecture.objects.get(id=note.lecture_id)
        if _lecture_timeout_locked(lecture) and status != NoteGenerationStatus.FAILED:
            return
        # Stages report their own milestones and several of them (slide
        # grounding, repairs) run after the writing phase, so a later stage can
        # carry a smaller number. Within one run progress only moves forward;
        # a value of 10 or below marks the start of a fresh run.
        if status != NoteGenerationStatus.FAILED and progress > 10:
            # The stored row is the authority, not this in-memory copy: topic
            # writing, budget condensing and formula verification all run on
            # worker threads whose progress messages are applied here, and a
            # provider event (which reports a fixed, low number of its own) must
            # never pull the bar back behind what the writing phase reached.
            stored_progress = (
                GeneratedNote.objects.filter(pk=note.pk)
                .values_list("progress", flat=True)
                .first()
            )
            progress = max(progress, int(note.progress or 0), int(stored_progress or 0))
        note.status = status
        note.progress = progress
        generation_metadata = dict(note.generation_metadata or {})
        if stage_detail:
            generation_metadata["current_stage_detail"] = stage_detail
        if stage_message is not None:
            generation_metadata["current_stage_message"] = stage_message
        if stage_detail or stage_message is not None:
            trace = list(generation_metadata.get("stage_trace", []))
            next_entry = {
                "status": status,
                "progress": progress,
                "stage_detail": stage_detail or generation_metadata.get("current_stage_detail", ""),
                "message": stage_message if stage_message is not None else generation_metadata.get("current_stage_message", ""),
                "timestamp": timezone.now().isoformat(),
            }
            if not trace or trace[-1] != next_entry:
                trace.append(next_entry)
            generation_metadata["stage_trace"] = trace
        extra["generation_metadata"] = {**generation_metadata, **dict(extra.get("generation_metadata") or {})}
        for key, value in extra.items():
            setattr(note, key, value)
        update_fields = ["status", "progress", "updated_at", *extra.keys()]
        note.save(update_fields=update_fields)
        self._touch_note_activity(
            lecture=lecture,
            note=note,
            stage_detail=stage_detail or generation_metadata.get("current_stage_detail", ""),
            stage_message=stage_message if stage_message is not None else generation_metadata.get("current_stage_message", ""),
        )
        lecture.refresh_from_db()
        if not _lecture_timeout_locked(lecture):
            LectureProcessingStateService.mark_generating_notes(
                lecture=lecture,
                progress=max(lecture.processing_progress, progress),
            )

    def fail(
        self,
        *,
        lecture: Lecture,
        model_name: str,
        error: PipelineError,
        expected_generation: int | None = None,
    ) -> None:
        if not ProcessingJobService.is_current_execution(
            lecture_id=str(lecture.id), expected_generation=expected_generation
        ):
            logger.warning(
                "note_generation_fail_skipped_fenced_attempt",
                extra={"lecture_id": str(lecture.id), "caller_generation": expected_generation},
            )
            return
        try:
            note = self.get_or_create_note(lecture=lecture, model_name=model_name)
            ErrorRecordingService.record_processing_error(
                user=lecture.user,
                lecture=lecture,
                stage=error.stage,
                error_type=error.__class__.__name__,
                error_message=error.message,
                metadata={"model_name": model_name},
            )
            ProcessingJobService.mark_failed_for_lecture(
                lecture_id=str(lecture.id),
                stage=error.stage,
                expected_generation=expected_generation,
            )
            LectureProcessingStateService.mark_failed(lecture=lecture, error_message=error.message)
            note.status = NoteGenerationStatus.FAILED
            note.progress = 100
            note.error_message = error.message
            note.generation_metadata = {
                **note.generation_metadata,
                "stage": error.stage,
                "error_message": error.message,
                "provider": model_name,
                "current_stage_detail": "FAILED",
                "current_stage_message": error.message,
                "stage_trace": [
                    *list((note.generation_metadata or {}).get("stage_trace", [])),
                    {
                        "status": NoteGenerationStatus.FAILED,
                        "progress": 100,
                        "stage_detail": "FAILED",
                        "message": error.message,
                        "timestamp": timezone.now().isoformat(),
                    },
                ],
            }
            note.save(update_fields=["status", "progress", "error_message", "generation_metadata", "updated_at"])
            lecture.refresh_from_db()
            lecture.processing_metadata = {
                **(lecture.processing_metadata or {}),
                "note_generation_last_activity_at": timezone.now().isoformat(),
                "note_generation_stage": "FAILED",
                "note_generation_stage_message": error.message,
            }
            lecture.save(update_fields=["processing_metadata", "updated_at"])
        finally:
            close_old_connections()

    def run_pipeline(
        self,
        *,
        lecture: Lecture,
        transcript_override: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        override_text = str(transcript_override or "").strip()
        transcript_exists = bool(override_text) or bool(lecture.merged_transcript.strip()) or lecture.transcript_chunks.filter(
            text__gt=""
        ).exists()
        if not transcript_exists:
            raise PipelineError(stage="NOTE_GENERATION", message="Transcript is empty; note generation cannot start.")

        lecture.refresh_from_db()
        if not _lecture_timeout_locked(lecture):
            LectureProcessingStateService.mark_generating_notes(
                lecture=lecture,
                progress=max(lecture.processing_progress, 1),
            )

        composed = self.composer.build(lecture=lecture, transcript_override=override_text or None)
        with HeartbeatService.keepalive(lecture_id=str(lecture.id)):
            for model_name in self.models:
                try:
                    self._run_model_pipeline(lecture=lecture, model_name=model_name, composed=composed)
                except PipelineError as exc:
                    self.fail(
                        lecture=lecture,
                        model_name=model_name,
                        error=exc,
                        expected_generation=expected_generation,
                    )
                except Exception as exc:
                    self.fail(
                        lecture=lecture,
                        model_name=model_name,
                        error=PipelineError(stage="SYSTEM", message=str(exc)),
                        expected_generation=expected_generation,
                    )

        overall_status, _ = aggregate_note_status(lecture=lecture)
        lecture.refresh_from_db()
        usage_log = UsageLogService.get_latest_for_lecture(lecture=lecture)
        if not ProcessingJobService.is_current_execution(
            lecture_id=str(lecture.id), expected_generation=expected_generation
        ):
            # Phase 12 fencing: recovery already reclaimed this job while this
            # (now obsolete) attempt was still running. Do not mark the lecture
            # completed/failed, finalize usage, or dispatch follow-up work —
            # the current attempt owns all of that.
            logger.warning(
                "note_generation_finalize_skipped_fenced_attempt",
                extra={"lecture_id": str(lecture.id), "caller_generation": expected_generation},
            )
            return NoteGenerationDebugService.debug_payload(lecture=lecture)
        if _lecture_timeout_locked(lecture):
            LectureProcessingStateService.mark_failed(
                lecture=lecture,
                error_message=lecture.processing_error or "Lecture processing timed out.",
            )
        elif overall_status == NoteGenerationStatus.COMPLETED:
            with measure_scaling_stage(stage="processing_finalization", lecture_id=str(lecture.id)):
                ProcessingJobService.mark_finalizing_for_lecture(
                    lecture_id=str(lecture.id), expected_generation=expected_generation
                )
                LectureProcessingStateService.mark_completed(lecture=lecture)
                if usage_log is not None:
                    UsageLogService.finalize_processing_once(usage_log=usage_log)
                selected_outputs = {
                    str(output)
                    for output in (lecture.processing_metadata or {}).get("study_outputs", []) or []
                }
                if selected_outputs.intersection({"summary", "flashcards", "quiz"}):
                    # Keep selected outputs on the established notes queue. This also makes a
                    # selected summary resilient when a worker is upgraded during processing.
                    from apps.notes.tasks import generate_selected_study_items_task

                    generate_selected_study_items_task.delay(str(lecture.id))
                ProcessingJobService.mark_succeeded_for_lecture(
                    lecture_id=str(lecture.id), expected_generation=expected_generation
                )
                if should_generate_comparison_v2_note(user=lecture.user):
                    # Admin-only second note; its own task so it never delays or fails this lecture.
                    from apps.notes.tasks import generate_comparison_v2_note_task

                    generate_comparison_v2_note_task.delay(str(lecture.id))
        elif overall_status == NoteGenerationStatus.FAILED:
            LectureProcessingStateService.mark_failed(lecture=lecture, error_message="Note generation failed.")
            if usage_log is not None:
                UsageLogService.fail_processing_once(usage_log=usage_log, error_message="Note generation failed.")
        return NoteGenerationDebugService.debug_payload(lecture=lecture)

    def run_comparison_v2_note(self, *, lecture: Lecture) -> GeneratedNote | None:
        """Admin comparison: write a second note for a finished lecture with the v2 pipeline.

        Runs only for accounts allowed by should_generate_comparison_v2_note. The note is
        stored under its own variant, so it never replaces or overwrites the lecture's
        normal note, never changes the lecture's status and is never billed; a failure is
        recorded on the comparison note alone.
        """
        if not should_generate_comparison_v2_note(user=lecture.user):
            return None
        lecture.refresh_from_db()
        if lecture.status != LectureStatus.COMPLETED:
            return None
        model_name = settings.NOTE_GENERATION_V2_MODEL
        variant = comparison_v2_variant(model_name)
        try:
            composed = self.composer.build(lecture=lecture)
            note = self._run_model_pipeline(
                lecture=lecture, model_name=model_name, composed=composed, pipeline="v2", variant=variant
            )
        except Exception as exc:
            message = str(getattr(exc, "message", "") or exc)[:500]
            logger.warning(
                "note_comparison_v2_failed",
                extra={"lecture_id": str(lecture.id), "error": message},
                exc_info=not isinstance(exc, PipelineError),
            )
            GeneratedNote.objects.filter(lecture=lecture, variant=variant).update(
                status=NoteGenerationStatus.FAILED, progress=100, error_message=message, updated_at=timezone.now()
            )
            return None
        label_comparison_titles(lecture=lecture, comparison_note=note)
        note.refresh_from_db()
        return note

    def run_alt_version(self, *, lecture: Lecture, style: str) -> GeneratedNote | None:
        """Write the lecture's note again in another style, as an extra note of the same lecture.

        It never replaces the lecture's own note and never changes the lecture's status. The
        wallet charge (taken when the student asked) is refunded if the version fails.
        """
        from apps.notes.note_styles import STYLE_TITLE_LABELS, alt_variant, normalize_style

        style = normalize_style(style)
        if not style:
            return None
        lecture.refresh_from_db()
        if lecture.status != LectureStatus.COMPLETED:
            return None
        model_name = self.models[0] if self.models else settings.NOTE_PRIMARY_MODEL
        variant = alt_variant(normalize_note_variant(model_name), style)
        try:
            composed = self.composer.build(lecture=lecture)
            note = self._run_model_pipeline(
                lecture=lecture, model_name=model_name, composed=composed, pipeline="alt", variant=variant, style=style
            )
        except Exception as exc:
            message = str(getattr(exc, "message", "") or exc)[:500]
            logger.warning(
                "note_alt_version_failed",
                extra={"lecture_id": str(lecture.id), "style": style, "error": message},
                exc_info=not isinstance(exc, PipelineError),
            )
            GeneratedNote.objects.filter(lecture=lecture, variant=variant).update(
                status=NoteGenerationStatus.FAILED, progress=100, error_message=message, updated_at=timezone.now()
            )
            from apps.notes.versions import refund_version_charge

            refund_version_charge(lecture=lecture, style=style)
            return None
        label = STYLE_TITLE_LABELS.get(style, "")
        title = str(note.title or lecture.title or "").strip()
        if label and not title.endswith(label):
            note.title = f"{title}{label}"[:255]
            note.save(update_fields=["title", "updated_at"])
        return note

    def _generate_clean_transcript_document(
        self, *, lecture: Lecture, context: dict[str, Any], provider: Any
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        from apps.audio_pipeline.models import AudioChunk
        from apps.notes.clean_transcript import clean_transcript_document
        from services.note_review.timeline import build_timeline

        to_lecture = lambda seconds: seconds  # noqa: E731
        try:
            metadata = lecture.processing_metadata if isinstance(lecture.processing_metadata, dict) else {}
            timeline, _ = build_timeline(
                list(AudioChunk.objects.filter(lecture=lecture)),
                raw_mappings=metadata.get("timestamp_mappings"),
                processed_duration=lecture.optimized_audio_duration,
                tolerance=float(getattr(settings, "NOTE_REVIEW_DURATION_TOLERANCE_SECONDS", 0.25)),
            )
            if timeline is not None:
                to_lecture = timeline.to_lecture
        except Exception:  # times are a reading aid; the text matters more than the clock
            logger.warning("clean_transcript_timeline_unavailable", extra={"lecture_id": str(lecture.id)}, exc_info=True)
        header = {
            "course_name": lecture.course_name or lecture.title,
            "course_detail": lecture.professor_name,
            "session_number": lecture.session_number,
            "lecture_badge": "",
        }
        persian = str(lecture.output_language or "fa") == "fa"
        document, usage, report = clean_transcript_document(
            provider=provider,
            chunks=list(context.get("transcript_chunks") or []),
            to_lecture_time=to_lecture,
            header=header,
            course=str(lecture.course_name or ""),
            title=str(lecture.title or ""),
            persian=persian,
        )
        return document, usage, {"clean_transcript": report, "provider_call_summary": {}}

    def _run_model_pipeline(
        self,
        *,
        lecture: Lecture,
        model_name: str,
        composed: dict[str, Any],
        pipeline: str | None = None,
        variant: str | None = None,
        style: str | None = None,
    ) -> GeneratedNote:
        """Write one note. pipeline="v2" forces the lecturer's-words pipeline (admin comparison).

        ``style`` is the note style (apps.notes.note_styles); the main note takes the lecture's
        chosen style, an extra version passes its own.
        """
        note = self.start(lecture=lecture, model_name=model_name, variant=variant, mark_lecture=pipeline is None)
        self._assert_lecture_not_locked(lecture_id=str(lecture.id))
        note.source_snapshot = {
            "lecture": composed["lecture"],
            "has_slides": composed["has_slides"],
            "transcript_chunk_count": len(composed["transcript_chunks"]),
            "slide_page_count": len(composed["slide_context"]["pages"]),
            "course_hints": composed["course_hints"],
            "resolved_theme": normalize_note_theme(lecture.processing_metadata.get("note_theme")),
            "input_language": lecture.input_language,
            "output_language": lecture.output_language,
            "note_detail_mode": lecture.note_detail_mode,
        }
        note.save(update_fields=["source_snapshot", "updated_at"])
        self._touch_note_activity(lecture=lecture, note=note, stage_detail="PREPARING_INPUT", stage_message="Preparing transcript, slide context, and course hints.")

        transcript_text = str(composed["transcript_text"])
        self.update(
            note=note,
            status=NoteGenerationStatus.STRUCTURING,
            progress=10,
            stage_detail="PREPARING_INPUT",
            stage_message="Preparing transcript, slide context, and course hints.",
            summary_text="",
        )

        semantic_document: dict[str, Any] | None = None
        semantic_usage: dict[str, int] | None = None
        semantic_report: dict[str, Any] = {}
        attempt_errors: list[dict[str, str]] = []
        semantic_generation_strategy = "direct"
        from apps.notes.note_styles import STYLE_LECTURER, STYLE_TRANSCRIPT, lecture_style

        note_style = style or (STYLE_LECTURER if pipeline == "v2" else lecture_style(lecture))
        transcript_mode = note_style == STYLE_TRANSCRIPT
        chosen_lecturer = note_style == STYLE_LECTURER and pipeline != "v2"
        force_v2 = pipeline == "v2" or note_style == STYLE_LECTURER
        teaching_mode = not force_v2 and not transcript_mode and should_use_generation_pipeline_v3(user=lecture.user)
        if teaching_mode and should_use_generation_pipeline_v4(user=lecture.user):
            composed["note_pedagogy"] = "v4"
        # The quality revision: "v4.1" for every user's main v4 note, "v2.1" (lecturer's words,
        # comparison note) for the staff accounts listed in NOTE_QUALITY_V21_PHONE_NUMBERS.
        # Revision 3 ("v4.2" / "v2.2") comes first: the staff accounts listed in
        # NOTE_QUALITY_V42_PHONE_NUMBERS get it for both notes; everyone else keeps revision 2.
        teaching_v4_now = composed.get("note_pedagogy") == "v4"
        if should_use_quality_rev_v42(user=lecture.user, teaching_v4=teaching_v4_now):
            composed["quality_rev"] = QUALITY_REV_V42
        elif should_use_quality_rev_v41(user=lecture.user, teaching_v4=teaching_v4_now) or should_use_quality_rev(
            user=lecture.user
        ):
            composed["quality_rev"] = QUALITY_REV
        if chosen_lecturer and getattr(settings, "NOTE_QUALITY_V42_ENABLED", False):
            # A student who chose "the lecturer's words" gets the current lecturer-words revision (v2.2).
            composed["quality_rev"] = QUALITY_REV_V42
        if transcript_mode:
            composed.pop("quality_rev", None)
        note.generation_metadata = {**(note.generation_metadata or {}), "note_style": note_style}
        note.save(update_fields=["generation_metadata", "updated_at"])
        if composed.get("quality_rev"):
            note.source_snapshot = {**(note.source_snapshot or {}), "quality_rev": composed["quality_rev"]}
            note.save(update_fields=["source_snapshot", "updated_at"])
        provider_route_summary: dict[str, Any] = {}
        successful_model_name = model_name

        # Provider-level fallback stays first. If that route is exhausted, we allow a
        # short outer fallback chain across prevalidated note models so the lecture
        # can still complete without wasting the finished transcription.
        candidate_models = list(
            dict.fromkeys(
                [
                    model_name,
                    *selected_note_fallback_models_for_lecture(
                        lecture=lecture,
                        primary_model=model_name,
                    ),
                ]
            )
        )
        last_error: PipelineError | None = None
        for candidate_model in candidate_models:
            provider = self.provider_factory(candidate_model)
            try:
                self.update(
                    note=note,
                    status=NoteGenerationStatus.STRUCTURING,
                    progress=30,
                    stage_detail="REQUESTING_MODEL",
                    stage_message=f"Requesting semantic note structure from {candidate_model}.",
                )
                if transcript_mode:
                    semantic_generation_strategy = "clean_transcript"
                    semantic_document, semantic_usage, semantic_report = self._generate_clean_transcript_document(
                        lecture=lecture, context=composed, provider=provider
                    )
                elif teaching_mode:
                    semantic_generation_strategy = (
                        "professor_v4" if composed.get("note_pedagogy") == "v4" else "professor_v3"
                    )
                    semantic_document, semantic_usage, semantic_report = self._generate_semantic_document_structured(
                        lecture=lecture,
                        note=note,
                        context=composed,
                        provider=provider,
                        model_name=candidate_model,
                        event_callback=self._build_provider_event_callback(note=note, model_name=candidate_model),
                        teaching_mode=True,
                    )
                elif force_v2 or should_use_generation_pipeline_v2(user=lecture.user):
                    semantic_generation_strategy = "structured_v2"
                    semantic_document, semantic_usage, semantic_report = self._generate_semantic_document_structured(
                        lecture=lecture,
                        note=note,
                        context=composed,
                        provider=provider,
                        model_name=candidate_model,
                        event_callback=self._build_provider_event_callback(note=note, model_name=candidate_model),
                    )
                elif self._should_use_chunked_semantic_generation(context=composed):
                    semantic_generation_strategy = "chunked"
                    semantic_document, semantic_usage, semantic_report = self._generate_semantic_document_chunked(
                        lecture=lecture,
                        note=note,
                        context=composed,
                        provider=provider,
                        model_name=candidate_model,
                        event_callback=self._build_provider_event_callback(note=note, model_name=candidate_model),
                    )
                else:
                    semantic_generation_strategy = "direct"
                    semantic_document, semantic_usage, semantic_report = self._generate_semantic_document_direct(
                        lecture=lecture,
                        context=composed,
                        provider=provider,
                        event_callback=self._build_provider_event_callback(note=note, model_name=candidate_model),
                    )
                provider_route_summary = dict(semantic_report.get("provider_call_summary", {}) or {})
                if attempt_errors:
                    prior_attempts = [
                        {
                            "provider": "model-selection",
                            "model": item["model"],
                            "attempt": 1,
                            "latency_ms": 0,
                            "outcome": "failed",
                            "error_type": "model_route_failed",
                            "error_message": item["message"],
                            "status_code": None,
                            "retryable": False,
                        }
                        for item in attempt_errors
                    ]
                    provider_route_summary["attempts"] = [
                        *prior_attempts,
                        *list(provider_route_summary.get("attempts", []) or []),
                    ]
                provider_route_summary["fallback_used"] = bool(
                    provider_route_summary.get("fallback_used", False)
                ) or candidate_model != model_name or bool(attempt_errors)
                successful_model_name = candidate_model
                break
            except PipelineError as exc:
                attempt_errors.append({"model": candidate_model, "message": exc.message})
                last_error = exc
                continue

        self._assert_lecture_not_locked(lecture_id=str(lecture.id))

        if semantic_document is None or semantic_usage is None:
            if last_error is None:
                raise PipelineError(stage="NOTE_GENERATION", message="No note-generation provider succeeded.")
            aggregated = " | ".join(f"{item['model']}: {item['message']}" for item in attempt_errors)
            raise PipelineError(stage=last_error.stage, message=aggregated)

        semantic_document = self._normalize_semantic_document(
            payload=semantic_document,
            lecture=lecture,
            merged_summary={},
        )
        if transcript_mode:
            # The class text is the lecturer's words with misheard words corrected: no block repair,
            # enrichment or example rebuilding (each of those adds or rewrites content).
            block_repair_report = {"skipped_reason": "transcript"}
            enrichment_report = {"skipped_reason": "transcript"}
            example_repair_report = {"skipped_reason": "transcript"}
            example_repair_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        semantic_document, block_repair_report = (semantic_document, block_repair_report) if transcript_mode else self._repair_semantic_blocks(
            lecture=lecture,
            note=note,
            model_name=successful_model_name,
            provider=self.provider_factory(successful_model_name),
            document=semantic_document,
            event_callback=self._build_provider_event_callback(
                note=note,
                model_name=successful_model_name,
            ),
        )
        self.update(
            note=note,
            status=NoteGenerationStatus.STRUCTURING,
            progress=55,
            stage_detail="VALIDATING_OUTPUT",
            stage_message="Normalizing and validating semantic note structure.",
        )
        semantic_document, enrichment_report = (semantic_document, enrichment_report) if transcript_mode else self.enricher.enrich_document(
            document=semantic_document,
            lecture=lecture,
            context=composed,
            # Boxes assembled from raw transcript sentences only suit the old v1
            # pipelines; in v2 (the lecturer's own words) they repeat sentences the
            # note already contains, and the teaching pipelines write their own.
            synthesize_blocks=semantic_generation_strategy in {"direct", "chunked"},
        )
        if transcript_mode:
            pass
        elif teaching_mode or quality_rev_v42(composed):
            # Example repair rebuilds examples from the lecturer's transcript wording, which is
            # exactly what the teaching pipeline replaces. Revision 3 (v4.2 / v2.2) never adds an
            # example or a solution the lecturer did not give, so the v2.2 note skips it too: the
            # reviews found reconstructed worked solutions and «cannot be confirmed» notices there.
            example_repair_report = {"skipped_reason": "teaching_mode" if teaching_mode else "quality_rev_v42"}
            example_repair_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        else:
            semantic_document, example_repair_report, example_repair_usage = self._repair_incomplete_examples(
                lecture=lecture,
                note=note,
                provider=self.provider_factory(successful_model_name),
                document=semantic_document,
                context=composed,
                event_callback=self._build_provider_event_callback(
                    note=note,
                    model_name=successful_model_name,
                ),
            )
        for usage_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            semantic_usage[usage_key] = int(semantic_usage.get(usage_key, 0) or 0) + int(
                example_repair_usage.get(usage_key, 0) or 0
            )
        self.update(
            note=note,
            status=NoteGenerationStatus.STRUCTURING,
            progress=68,
            stage_detail="ENRICHING_STRUCTURE",
            stage_message="Enhancing the note structure with introductions, examples, and summary blocks.",
        )

        self.update(
            note=note,
            status=NoteGenerationStatus.RENDERING,
            progress=80,
            stage_detail="RENDERING_BODY_HTML",
            stage_message="Rendering semantic note content into the HTML body.",
            title=str(semantic_document.get("document_title") or lecture.title),
            semantic_payload=semantic_document,
        )

        output_language = lecture.output_language or "fa"
        output_direction = "rtl" if is_rtl_language(output_language) else "ltr"
        body_html = GeneratedNoteService.render_body_from_semantic_document(
            document=semantic_document,
            language_code=output_language,
        )
        self._assert_lecture_not_locked(lecture_id=str(lecture.id))
        resolved_theme = normalize_note_theme(lecture.processing_metadata.get("note_theme"))
        full_html = GeneratedNoteService.render_full_html_from_body(
            body_content=body_html,
            theme=resolved_theme,
            language_code=output_language,
            direction=output_direction,
        )
        self.update(
            note=note,
            status=NoteGenerationStatus.RENDERING,
            progress=92,
            stage_detail="SAVING_HTML_FILE",
            stage_message="Saving the final HTML note file.",
            body_html=body_html,
        )
        html_relative_path = self._persist_html(
            lecture=lecture, model_name=model_name, html=full_html, variant=note.variant
        )
        note.body_html = body_html
        note.html_path = html_relative_path
        note.pdf_path = ""
        note.status = NoteGenerationStatus.RENDERING
        note.progress = 96
        note.generation_metadata = {
            **(note.generation_metadata or {}),
            "current_stage_detail": "GENERATING_PDF",
            "current_stage_message": "Generating the final PDF note file.",
            "stage_trace": [
                *list((note.generation_metadata or {}).get("stage_trace", [])),
                {
                    "status": NoteGenerationStatus.RENDERING,
                    "progress": 96,
                    "stage_detail": "GENERATING_PDF",
                    "message": "Generating the final PDF note file.",
                    "timestamp": timezone.now().isoformat(),
                },
            ],
        }
        note.save(
            update_fields=[
                "body_html",
                "html_path",
                "pdf_path",
                "status",
                "progress",
                "generation_metadata",
                "updated_at",
            ]
        )
        # The student reads the HTML note; the PDF is only needed for download. With the
        # deferred flag the note completes as soon as the HTML is saved and the PDF is
        # rendered by its own task (a PDF failure then no longer fails the note).
        pdf_deferred = bool(getattr(settings, "NOTE_PDF_DEFERRED_ENABLED", False))
        if pdf_deferred:
            pdf_relative_path = ""
        else:
            with measure_scaling_stage(
                stage="pdf_render",
                lecture_id=str(lecture.id),
                input_size_bytes=len(body_html.encode("utf-8")),
            ):
                pdf_relative_path = GeneratedNoteService.ensure_pdf_artifact(note)
            self._assert_lecture_not_locked(lecture_id=str(lecture.id))
            if not pdf_relative_path:
                raise PipelineError(stage="GENERATING_PDF", message="PDF generation failed for the note artifact.")

        note.generation_metadata = {
            "stage": "COMPLETED",
            "note_style": note_style,
            "clean_transcript": semantic_report.get("clean_transcript", {}) if transcript_mode else {},
            "provider": str(provider_route_summary.get("provider_used", "") or ""),
            "primary_provider": "gapgpt",
            "requested_model_name": model_name,
            "model_used": str(provider_route_summary.get("model_used", "") or model_name),
            "fallback_used": bool(provider_route_summary.get("fallback_used", False)),
            "generation_latency_ms": int(provider_route_summary.get("generation_latency_ms", 0) or 0),
            "retry_count": int(provider_route_summary.get("retry_count", 0) or 0),
            "provider_attempts": provider_route_summary.get("attempts", []) or attempt_errors,
            "usage": {
                "semantic_document": semantic_usage,
                "total_prompt_tokens": semantic_usage["prompt_tokens"],
                "total_completion_tokens": semantic_usage["completion_tokens"],
                "total_tokens": semantic_usage["total_tokens"],
            },
            "housekeeping_ratio_hint": self.composer.likely_housekeeping_ratio(text=transcript_text),
            "resolved_theme": resolved_theme,
            "input_language": lecture.input_language,
            "output_language": output_language,
            "note_detail_mode": lecture.note_detail_mode,
            "transcription_language_used": stt_language_code(lecture.input_language),
            "output_direction": output_direction,
            "summary_mode_enabled": lecture.note_detail_mode == "summarized",
            "token_optimization": {
                "body_only_generation": True,
                "semantic_renderer": True,
                "static_template_reuse": True,
                "direct_semantic_generation": semantic_generation_strategy == "direct",
                "chunked_semantic_generation": semantic_generation_strategy == "chunked",
            },
            "semantic_generation_strategy": semantic_generation_strategy,
            "generation_strategy": semantic_generation_strategy,
            "topic_outline": semantic_report.get("topic_outline", []),
            "structure_timings": semantic_report.get("structure_timings", {}),
            "batch_count": semantic_report.get("batch_count", 1),
            "concept_inventory": semantic_report.get("concept_inventory", []),
            "concept_coverage_report": semantic_report.get("concept_coverage_report", {}),
            "dedup_report": semantic_report.get("dedup_report", {}),
            "slide_reference_coverage": semantic_report.get("slide_reference_coverage", {}),
            "repair_pass_count": semantic_report.get("repair_pass_count", 0)
            + int(example_repair_report.get("attempted_count", 0) or 0),
            "provider_call_summary": provider_route_summary,
            "semantic_enrichment": enrichment_report,
            "example_reconstruction": example_repair_report,
            "semantic_block_repair": block_repair_report,
            "summary_compaction": semantic_report.get("summary_compaction_report", {}),
            # v3 only: what the planner decided to teach, how much room each
            # topic got, and whether the note had to be condensed to fit. This
            # is how we tell "the note is the right size" from "the note
            # happened to come out small this time".
            "teaching_plan": semantic_report.get("teaching_plan", {}),
            "teaching_budget": semantic_report.get("teaching_budget", {}),
            "teaching_selection_audits": semantic_report.get("teaching_selection_audits", []),
            "teaching_thin_topic_expansions": semantic_report.get(
                "teaching_thin_topic_expansions", []
            ),
            "teaching_budget_enforcement": semantic_report.get(
                "teaching_budget_enforcement", {}
            ),
            "quality_rev": self._quality_rev_report(
                context=composed, document=semantic_document, transcript_text=transcript_text
            ),
            "pdf_artifact_version": GeneratedNoteService.PDF_ARTIFACT_VERSION,
            "pdf_artifact_fingerprint": (note.generation_metadata or {}).get("pdf_artifact_fingerprint", ""),
            "html_artifact_fingerprint": (note.generation_metadata or {}).get("html_artifact_fingerprint", ""),
            "current_stage_detail": "COMPLETED",
            "current_stage_message": "HTML note was generated successfully."
            if pdf_deferred
            else "HTML note and PDF were generated successfully.",
            "pdf_deferred": pdf_deferred,
            "stage_trace": [
                *list((note.generation_metadata or {}).get("stage_trace", [])),
                {
                    "status": NoteGenerationStatus.COMPLETED,
                    "progress": 100,
                    "stage_detail": "COMPLETED",
                    "message": "HTML note was generated successfully."
                    if pdf_deferred
                    else "HTML note and PDF were generated successfully.",
                    "timestamp": timezone.now().isoformat(),
                },
            ],
        }
        note.prompt_metadata = {
            "has_slides": composed["has_slides"],
            "course_hints": composed["course_hints"],
            "model_name": str(provider_route_summary.get("model_used", "") or model_name),
            "requested_model_name": model_name,
            "transcript_chunk_count": len(composed["transcript_chunks"]),
            "transcript_source": composed.get("transcript_source", ""),
            "resolved_theme": resolved_theme,
            "input_language": lecture.input_language,
            "output_language": output_language,
            "note_detail_mode": lecture.note_detail_mode,
            "output_direction": output_direction,
        }
        note.body_html = body_html
        note.html_path = html_relative_path
        note.pdf_path = pdf_relative_path
        note.status = NoteGenerationStatus.COMPLETED
        note.progress = 100
        note.error_message = ""
        note.save(
            update_fields=[
                "generation_metadata",
                "prompt_metadata",
                "body_html",
                "html_path",
                "pdf_path",
                "status",
                "progress",
                "error_message",
                "updated_at",
            ]
        )
        lecture.refresh_from_db()
        lecture.processing_metadata = {
            **(lecture.processing_metadata or {}),
            "note_generation_last_activity_at": timezone.now().isoformat(),
            "note_generation_stage": "COMPLETED",
            "note_generation_stage_message": "HTML note and PDF were generated successfully.",
            "input_language": lecture.input_language,
            "output_language": output_language,
            "note_detail_mode": lecture.note_detail_mode,
            "output_direction": output_direction,
            "summary_mode_enabled": lecture.note_detail_mode == "summarized",
        }
        lecture.save(update_fields=["processing_metadata", "updated_at"])
        try:
            from services.note_review.eligibility import capture_enabled_for_user_id

            if capture_enabled_for_user_id(note.user_id):
                from apps.notes.tasks import compute_note_review_task

                compute_note_review_task.delay(str(note.id))
        except Exception:
            # The accuracy check is optional; it must never affect note delivery.
            logger.exception("note_review_dispatch_failed", extra={"note_id": str(note.id)})
        if pdf_deferred:
            try:
                from apps.notes.tasks import generate_note_pdf_task

                generate_note_pdf_task.delay(str(note.id))
            except Exception:
                # The viewer and the download endpoints queue the same task on demand.
                logger.exception("note_pdf_dispatch_failed", extra={"note_id": str(note.id)})
        return note

    def _generate_semantic_document_direct(
        self,
        *,
        lecture: Lecture,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        system_prompt = (
            "You are a source-grounded lecture-note editor. "
            "You are NOT producing a transcript dump. "
            "Transform only the professor's transcribed speech and uploaded slide evidence into polished, student-ready notes with clear hierarchy. "
            f"{self._language_instruction(context=context)} "
            f"{self._detail_mode_instruction(context=context)} "
            "Do NOT add external textbook definitions, examples, explanations, or prior knowledge. "
            "Do NOT define a term unless the professor explicitly defined or explained it, or the uploaded slides directly state that definition. "
            "If a term is only mentioned, mention it briefly without inventing an explanation. "
            "Never generate CSS, head tags, scripts, or full HTML. "
            "Use the semantic block DSL only. "
            f"{self._source_selection_instruction(context=context)} "
            f"{self._removal_scope_instruction(context=context)} "
            "Rewrite spoken language into formal written prose in the selected output language instead of copying long raw transcript spans. "
            "Use structure only when supported by the source. "
            "Repair broken transcript terms using slides and course context when needed, but do not let slides introduce unrelated explanations. "
            "Treat worked examples as evidence-sensitive content: combine all nearby transcript sentences and matching slide OCR, formulas, and tables before writing the example. "
            "When speech refers to writing on a board, reconstruct omitted steps only when they are uniquely determined by that evidence. "
            "If exact board-only values are unavailable, never invent them; provide a complete symbolic/generalized worked example and label it as reconstructed, or state the missing visual dependency. "
            "Return JSON only."
        )
        user_prompt = (
            "Schema rules:\n"
            "- Root keys: document_title, pages\n"
            "- pages is an array\n"
            "- each page has header, blocks, footer\n"
            f"- valid block types: {', '.join(SemanticNoteRenderer.supported_block_types())}\n"
            "- all list.items values must be plain strings only, never objects\n"
            "- prefer plain text fields with lightweight inline markers instead of content_html\n"
            "- allowed inline markers inside text fields only: **term** for bold, [[key:term]] for theme-color emphasis, [[label:نکته:]] or [[label:مثال:]] for red labels\n"
            "- use content_html only in rare cases when a simple inline marker cannot express the needed inline styling\n"
            "- use figure-reference blocks instead of inventing diagrams\n"
            "- when the lecture refers to a shape, table, diagram, or slide-backed visual concept, connect that explanation to the relevant uploaded slide page whenever it is reasonably inferable from the slide context\n"
            "- keep text in the selected output language, but keep essential technical terms when necessary\n"
            f"- {self._language_instruction(context=context)}\n"
            f"- {self._detail_mode_instruction(context=context)}\n"
            "- when transcript has weak wording, repair it with the course context instead of deleting it\n"
            f"{self._direct_mode_rules(context=context)}"
            f"{self._direct_structure_rules(context=context)}"
            "- do not add definitions, explanations, examples, caveats, or summaries that are not present in the transcript or uploaded slide evidence\n"
            "- if a term appears without explanation, mention it briefly without expanding it into a textbook definition\n"
            "- DO NOT output a raw transcript or speaker-by-speaker text\n"
            "- DO NOT rely on paragraphs alone when the lecture is long enough to support richer structure\n"
            "- NEVER invent a new block type outside the valid block types list\n"
            "- if content looks like bullet points or enumerated items, use the list block\n"
            "- the final page should normally end with a summary-box containing 3 to 6 concise takeaways\n"
            "- for a coherent spoken segment that is unrelated to the course learning content, use unrelated-discussion-box; keep it concise and do not mix it into an academic section or the final summary\n"
            "- use unrelated-discussion-box only for casual, personal, or administrative talk that a student can safely skip; never use it for taught content, instructions needed to complete coursework, assessment requirements, or course safety information\n"
            "- use section-header or subsection-header to break topic transitions instead of producing one long block of prose\n"
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"Course hints: {json.dumps(context['course_hints'], ensure_ascii=False)}\n"
            f"Slide context:\n{self._slide_context_snippet(context=context)}\n"
            f"Transcript chunk outline:\n{self._transcript_chunk_snippet(context=context)}\n"
            f"Full transcript text:\n{context['transcript_text']}"
        )
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        else:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        summary_compaction_report: dict[str, Any] = {"enabled": False}
        if self._note_detail_mode(context=context) == "summarized":
            payload, summary_compaction_report = self._compact_summarized_document(
                document=payload,
                context=context,
            )
        self._validate_semantic_document(payload)
        direct_batch = {
            "batch_index": 1,
            "chunk_indexes": [chunk.get("chunk_index", 0) for chunk in context.get("transcript_chunks", [])],
            "start_ts": 0.0,
            "end_ts": float((context.get("transcript_chunks", []) or [{}])[-1].get("end_ts", 0.0) or 0.0),
            "text": str(context.get("transcript_text", "") or ""),
        }
        return payload, llm_result.usage, {
            "batch_count": 1,
            "concept_inventory": context.get("concept_inventory", [])[:20],
            "concept_coverage_report": {"mode": "direct", "missing_concepts": [], "covered_concepts": []},
            "dedup_report": {"merged_unit_count": 0, "dropped_duplicate_units": 0},
            "note_generation_input_audit": self._build_note_generation_input_audit(
                context=context,
                batches=[direct_batch],
            ),
            "unit_grounding_report": {"mode": "direct_semantic_blocks"},
            "summary_compaction_report": summary_compaction_report,
            "slide_reference_coverage": {"blocks_with_slide_reference": 0, "total_blocks": 0},
            "repair_pass_count": 0,
            "provider_call_summary": summarize_completion_results([llm_result]),
        }

    def _generate_semantic_document_chunked(
        self,
        *,
        lecture: Lecture,
        note: GeneratedNote,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        model_name: str,
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        batches = self.composer.transcript_semantic_batches(
            transcript_chunks=context["transcript_chunks"],
            transcript_text=str(context["transcript_text"]),
            max_chars=self._semantic_batch_char_limit(context=context),
        )
        if not batches:
            raise PipelineError(stage="NOTE_GENERATION", message="Transcript is empty after chunk preparation.")

        total_batches = len(batches)
        generation_input_audit = self._build_note_generation_input_audit(
            context=context,
            batches=batches,
        )
        has_slides = bool(context.get("has_slides"))
        content_units: list[dict[str, Any]] = []
        aggregate_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        completion_results: list[Any] = []
        document_title = str(context["lecture"].get("title") or lecture.title)
        retrieved_slide_page_numbers: set[int] = set()
        grounding_reports: list[dict[str, Any]] = []

        total_batch_chars = max(1, sum(len(str(b.get("text", "") or "")) for b in batches))
        written_batch_chars = 0

        for batch_index, batch in enumerate(batches, start=1):
            progress = min(48, 20 + int(written_batch_chars / total_batch_chars * 28))
            self.update(
                note=note,
                status=NoteGenerationStatus.STRUCTURING,
                progress=progress,
                stage_detail="REQUESTING_MODEL",
                stage_message=f"Structuring part {batch_index} of {total_batches} with {model_name}.",
            )
            relevant_slide_pages = (
                self._retrieve_relevant_slide_pages(
                    context=context,
                    text=str(batch.get("text", "") or ""),
                    top_k=5,
                )
                if has_slides
                else []
            )
            retrieved_slide_page_numbers.update(
                int(page.get("page_number") or 0) for page in relevant_slide_pages if page.get("page_number")
            )
            batch_units, batch_grounding_reports, batch_llm_results, batch_payload = (
                self._units_with_recovery(
                    generate=partial(
                        self._generate_content_unit_batch,
                        lecture=lecture,
                        context=context,
                        provider=provider,
                        batch=batch,
                        batch_index=batch_index,
                        total_batches=total_batches,
                        relevant_slide_pages=relevant_slide_pages,
                        event_callback=event_callback,
                    ),
                    batch=batch,
                    relevant_slide_pages=relevant_slide_pages,
                    label=f"transcript part {batch_index}",
                    fallback_title=document_title,
                )
            )
            grounding_reports.extend(batch_grounding_reports)
            document_title = str(batch_payload.get("document_title") or document_title)
            for order_in_batch, unit in enumerate(batch_units):
                content_units.append(
                    self._normalize_content_unit(
                        unit=unit,
                        batch=batch,
                        batch_index=batch_index,
                        order_in_batch=order_in_batch,
                        relevant_slide_pages=relevant_slide_pages,
                    )
                )
            for llm_result in batch_llm_results:
                usage = llm_result.usage
                completion_results.append(llm_result)
                aggregate_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
                aggregate_usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
                aggregate_usage["total_tokens"] += int(usage.get("total_tokens", 0))
            written_batch_chars += len(str(batch.get("text", "") or ""))
            self.update(
                note=note,
                status=NoteGenerationStatus.STRUCTURING,
                progress=min(48, 20 + int(written_batch_chars / total_batch_chars * 28)),
                stage_detail="REQUESTING_MODEL",
                stage_message=f"Structured part {batch_index} of {total_batches}.",
            )

        if not content_units:
            raise PipelineError(
                stage="NOTE_GENERATION",
                message=f"{model_name} produced no usable concept units for this lecture.",
            )
        merged_units, dedup_report = self._merge_content_units(content_units=content_units)
        unit_pipeline_audit = {
            "model_output_units_before_merging": self._unit_debug_snapshot(content_units),
            "units_after_initial_merge": self._unit_debug_snapshot(merged_units),
            "units_after_transcript_repair": [],
            "units_after_missing_concept_repair": [],
            "units_after_definition_dedup": [],
            "final_semantic_blocks": [],
        }
        merged_units, transcript_rewrite_report, rewrite_usage = self._repair_transcript_alignment_units(
            lecture=lecture,
            note=note,
            provider=provider,
            context=context,
            merged_units=merged_units,
            event_callback=event_callback,
        )
        aggregate_usage["prompt_tokens"] += int(rewrite_usage.get("prompt_tokens", 0))
        aggregate_usage["completion_tokens"] += int(rewrite_usage.get("completion_tokens", 0))
        aggregate_usage["total_tokens"] += int(rewrite_usage.get("total_tokens", 0))
        unit_pipeline_audit["units_after_transcript_repair"] = self._unit_debug_snapshot(merged_units)
        summarized_mode = self._note_detail_mode(context=context) == "summarized"
        if has_slides and not summarized_mode:
            merged_units, concept_coverage_report, repair_pass_count, repair_usage = self._repair_missing_concepts(
                lecture=lecture,
                note=note,
                model_name=model_name,
                provider=provider,
                context=context,
                merged_units=merged_units,
                event_callback=event_callback,
            )
            aggregate_usage["prompt_tokens"] += int(repair_usage.get("prompt_tokens", 0))
            aggregate_usage["completion_tokens"] += int(repair_usage.get("completion_tokens", 0))
            aggregate_usage["total_tokens"] += int(repair_usage.get("total_tokens", 0))
        else:
            concept_coverage_report = {
                "inventory_size": len(context.get("concept_inventory", [])),
                "covered_concepts": [],
                "missing_concepts": [],
                "repaired_concepts": [],
                "skipped_reason": (
                    "summarized_mode_keeps_only_major_topics"
                    if summarized_mode
                    else "no_slides_uploaded"
                ),
            }
            repair_pass_count = 0

        unit_pipeline_audit["units_after_missing_concept_repair"] = self._unit_debug_snapshot(merged_units)
        merged_units, concept_consolidation_report = self._remove_repeated_definitions_safely(
            units=merged_units,
            context=context,
        )
        summary_compaction_report: dict[str, Any] = {
            "enabled": False,
            "input_unit_count": len(merged_units),
            "output_unit_count": len(merged_units),
        }
        if summarized_mode:
            merged_units, summary_compaction_report = self._compact_summarized_units(
                units=merged_units,
                context=context,
            )
        merged_units, formula_check_report, formula_usage = self._verify_unit_formulas(
            lecture=lecture, provider=provider, units=merged_units, event_callback=event_callback
        )
        for usage_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            aggregate_usage[usage_key] += int(formula_usage.get(usage_key, 0) or 0)
        unit_pipeline_audit["formula_verification"] = formula_check_report
        unit_pipeline_audit["units_after_definition_dedup"] = self._unit_debug_snapshot(merged_units)
        blocks = self._build_semantic_blocks_from_units(
            lecture=lecture,
            context=context,
            units=merged_units,
            document_title=document_title,
        )
        labels = semantic_labels_for_language(lecture.output_language or "fa")
        unit_pipeline_audit["final_semantic_blocks"] = self._block_debug_snapshot(blocks)
        document = {
            "document_title": document_title,
            "pages": [
                {
                    "header": {
                        "course_name": lecture.course_name,
                        "course_detail": lecture.professor_name,
                        "session_number": lecture.session_number,
                        "lecture_badge": "",
                    },
                    "blocks": blocks,
                    "footer": {
                        "page_number": f"{labels['page']} 1",
                        "watermark": "",
                    },
                }
            ],
        }
        self._validate_semantic_document(document)
        slide_reference_coverage = self._estimate_slide_reference_coverage(blocks=blocks)
        return document, aggregate_usage, {
            "batch_count": total_batches,
            "concept_inventory": context.get("concept_inventory", [])[:40],
            "concept_coverage_report": concept_coverage_report,
            "dedup_report": {**dedup_report, "transcript_rewrite_report": transcript_rewrite_report},
            "concept_consolidation_report": concept_consolidation_report,
            "summary_compaction_report": summary_compaction_report,
            "note_generation_input_audit": generation_input_audit,
            "unit_grounding_report": self._summarize_unit_grounding_reports(grounding_reports),
            "unit_pipeline_audit": unit_pipeline_audit,
            "slide_reference_coverage": {
                **slide_reference_coverage,
                "retrieved_slide_pages": sorted(number for number in retrieved_slide_page_numbers if number),
            },
            "repair_pass_count": repair_pass_count + int(transcript_rewrite_report.get("rewritten_unit_count", 0)),
            "provider_call_summary": summarize_completion_results(completion_results),
        }

    # -- Structure-first (v2) generation pipeline -----------------------------
    #
    # A/B-gated by should_use_generation_pipeline_v2(). Two LLM stages instead
    # of chunked mode's single pass per chronological batch:
    #   Stage A (_generate_topic_outline): one call sees the WHOLE transcript
    #       at once and returns an ordered topic outline, explicitly merging
    #       every time the professor returns to the same topic later in the
    #       lecture into one topic_id instead of leaving that to a post-hoc
    #       lexical-key merge of independently-written text.
    #   Stage B (_generate_content_unit_batch_for_topic): one call per topic,
    #       fed every transcript excerpt for that topic gathered together
    #       (even if scattered across the lecture), writing ONE coherent
    #       treatment of it.
    # From content_units onward this reuses the exact same merge/repair/
    # dedup/render tail as chunked mode -- those passes remain a safety net
    # for edge cases, not the primary consolidation mechanism.
    def _generate_semantic_document_structured(
        self,
        *,
        lecture: Lecture,
        note: GeneratedNote,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        model_name: str,
        event_callback: Any | None = None,
        teaching_mode: bool = False,
    ) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
        self.update(
            note=note,
            status=NoteGenerationStatus.STRUCTURING,
            progress=18,
            stage_detail="REQUESTING_MODEL",
            stage_message=f"Planning note structure for the whole lecture with {model_name}.",
        )
        outline_started_at = time.monotonic()
        try:
            outline_payload, outline_usage, outline_llm_result = self._generate_topic_outline(
                lecture=lecture,
                context=context,
                provider=provider,
                event_callback=event_callback,
                teaching_mode=teaching_mode,
            )
        except PipelineError as exc:
            if exc.stage != "NOTE_GENERATION":
                raise
            # A failed planning call must not fail the lecture: the empty plan
            # normalises to one topic covering the whole transcript, which the
            # writing stage already splits into parts on its own.
            logger.warning("note_generation_outline_failed_using_single_topic error=%s", exc.message)
            outline_payload = {}
            outline_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            outline_llm_result = None
        topics = self._normalize_topic_outline(
            payload=outline_payload, context=context, lecture=lecture, teaching_mode=teaching_mode
        )
        if not topics:
            raise PipelineError(
                stage="NOTE_GENERATION",
                message=f"{model_name} returned no usable topic outline for this lecture.",
            )
        if quality_rev_on(context):
            # No section about class talk or unclear speech; its chunks join the nearest topic.
            topics = dissolve_meta_topics(topics)
        if quality_rev_on(context) and not teaching_mode:
            # v2.1: every topic is one continuous stretch of the lecture, in the order it was spoken.
            topics = time_order_topics(
                topics,
                [int(chunk.get("chunk_index", 0)) for chunk in context.get("transcript_chunks", [])],
            )

        has_slides = bool(context.get("has_slides"))
        content_units: list[dict[str, Any]] = []
        aggregate_usage = dict(outline_usage)
        completion_results: list[Any] = [outline_llm_result] if outline_llm_result is not None else []
        # Decided once, from Stage A (which sees the whole lecture), and
        # never touched again. Letting every per-topic Stage B call
        # overwrite this (the old behavior) meant whichever topic was
        # processed LAST silently won -- if that happened to be an
        # off-topic or "other topics" catch-all, the whole note ended up
        # titled e.g. "گفت‌وگوی غیر درسی آغاز جلسه" or "منابع درس" instead
        # of anything related to the actual subject taught.
        document_title = (
            str(outline_payload.get("document_title") or "").strip()
            or str(context["lecture"].get("title") or lecture.title)
        )
        retrieved_slide_page_numbers: set[int] = set()
        grounding_reports: list[dict[str, Any]] = []
        selection_audits: list[dict[str, Any]] = []
        thin_topic_expansions: list[dict[str, Any]] = []
        total_topics = len(topics)
        # The note's size is decided here, before any writing, and handed to
        # each Stage B call as a share it may not exceed.
        teaching_plan: dict[str, Any] = {}
        budget_allocation: dict[str, Any] = {}
        if teaching_mode:
            teaching_plan = {
                "lecture_thesis": str(outline_payload.get("lecture_thesis") or "").strip(),
                "learning_objectives": self._audit_list(
                    outline_payload.get("learning_objectives")
                )[:7],
                "planner_omitted": self._audit_list(outline_payload.get("omitted"))[:60],
                "topics": [
                    {
                        "topic_id": topic.get("topic_id"),
                        "title": topic.get("title"),
                        "importance": topic.get("importance"),
                        "objective_ids": topic.get("objective_ids", []),
                        "refers_back_to": topic.get("refers_back_to", []),
                    }
                    for topic in topics
                ],
            }
            budget_allocation = self._allocate_teaching_budget(context=context, topics=topics)
        generation_input_audit = self._build_note_generation_input_audit(
            context=context,
            batches=[
                self._topic_pseudo_batch(context=context, topic=topic, topic_index=index)
                for index, topic in enumerate(topics, start=1)
            ],
        )

        # Progress tracks how much transcript text has actually been written
        # up, not how many topics have started: a single huge topic would
        # otherwise sit at one number for the entire (long) writing phase.
        sub_batches_by_topic = {
            topic_index: self._split_topic_into_sub_batches(
                context=context,
                topic=topic,
                topic_index=topic_index,
                teaching_mode=teaching_mode,
            )
            for topic_index, topic in enumerate(topics, start=1)
        }
        total_source_chars = max(
            1,
            sum(
                len(str(sub_batch.get("text", "") or ""))
                for sub_batches in sub_batches_by_topic.values()
                for sub_batch in sub_batches
            ),
        )
        writing_state = {"written_chars": 0}
        writing_state_lock = threading.Lock()
        total_parts = 0
        failed_parts = 0
        last_part_error = ""
        parallelism = self._effective_structure_parallelism(
            writable_topics=sum(1 for batches in sub_batches_by_topic.values() if batches)
        )
        writing_started_at = time.monotonic()
        timings: dict[str, Any] = {
            "structure_parallelism": parallelism,
            "outline_and_planning_seconds": round(writing_started_at - outline_started_at, 2),
        }

        def writing_progress() -> int:
            return min(48, 20 + int(writing_state["written_chars"] / total_source_chars * 28))

        topic_id_to_index = {
            str(topic.get("topic_id")): index for index, topic in enumerate(topics, start=1)
        }

        plan_digest_enabled = bool(getattr(settings, "NOTE_TOPIC_PLAN_DIGEST_ENABLED", False))
        # With every writable topic's concepts in the plan, nothing a topic is told depends on
        # another topic's output, so the topics need no window and can start heaviest first.
        fully_planned = plan_digest_enabled and all(
            topic.get("key_concepts")
            for topic_index, topic in enumerate(topics, start=1)
            if sub_batches_by_topic[topic_index]
        )

        def topic_weight(topic_index: int) -> float:
            return float(
                sum(len(str(sub_batch.get("text", "") or "")) for sub_batch in sub_batches_by_topic[topic_index])
            )

        def topic_dependencies(topic_index: int) -> set[int]:
            # A topic that explicitly refers back to an earlier one must see what
            # that topic actually established, so it waits for it. When the
            # planner already named that topic's concepts, the dependency is
            # satisfied by the plan and nothing has to wait.
            dependencies: set[int] = set()
            for reference in topics[topic_index - 1].get("refers_back_to") or []:
                dependency_index = topic_id_to_index.get(str(reference))
                if dependency_index is None or dependency_index >= topic_index:
                    continue
                if plan_digest_enabled and topics[dependency_index - 1].get("key_concepts"):
                    continue
                dependencies.add(dependency_index)
            return dependencies

        def digest_lines_for(topic_index: int, results: dict[int, dict[str, Any]]) -> list[str]:
            # Which earlier topics are visible is fixed by position alone (never
            # by which call happened to finish first), so a run is reproducible.
            # With parallelism 1 every earlier topic is visible.
            visible_until = topic_index - parallelism
            dependencies = topic_dependencies(topic_index)
            lines: list[str] = []
            for earlier_index in range(1, topic_index):
                earlier_plan = topics[earlier_index - 1]
                if plan_digest_enabled and earlier_plan.get("key_concepts") and sub_batches_by_topic[earlier_index]:
                    # Planned concepts depend on the outline only, so every topic
                    # gets the same "already covered" list however the calls
                    # happen to be scheduled.
                    lines.append(
                        f"- {earlier_plan['title']}: "
                        + "، ".join(earlier_plan["key_concepts"])
                    )
                elif earlier_index <= visible_until or earlier_index in dependencies:
                    digest_line = (results.get(earlier_index) or {}).get("digest_line")
                    if digest_line:
                        lines.append(digest_line)
                elif sub_batches_by_topic[earlier_index]:
                    earlier = topics[earlier_index - 1]
                    scope = str(earlier.get("scope") or "").strip()
                    lines.append(
                        f"- {earlier['title']} (being written at the same time as this topic, so its "
                        "details are not listed here; do not restate it, assume the reader has just "
                        "read it" + (f"; its scope: {scope}" if scope else "") + ")"
                    )
            return lines

        def write_topic(
            topic_index: int,
            base_digest_lines: list[str],
            emit: Any,
            part_event_callback: Any | None,
            abort: threading.Event | None,
        ) -> dict[str, Any]:
            topic = topics[topic_index - 1]
            topic_started_at = time.monotonic()
            result: dict[str, Any] = {
                "content_units": [],
                "grounding_reports": [],
                "selection_audits": [],
                "thin_expansions": [],
                "llm_results": [],
                "retrieved_pages": set(),
                "total_parts": 0,
                "failed_parts": 0,
                "last_error": "",
                "digest_line": "",
            }
            emit(
                f"Writing topic {topic_index} of {total_topics} ({topic['title']}) "
                f"with {model_name}."
            )
            topic_sub_batches = sub_batches_by_topic[topic_index]
            if not topic_sub_batches:
                return result
            total_sub_batches = len(topic_sub_batches)
            topic_anchor = f"topic-{topic.get('topic_id') or topic_index}"
            topic_order_counter = 0
            topic_concept_titles: list[str] = []

            for sub_batch_index, topic_batch in enumerate(topic_sub_batches, start=1):
                if abort is not None and abort.is_set():
                    raise _TopicWritingAborted()
                relevant_slide_pages = (
                    self._retrieve_relevant_slide_pages(
                        context=context,
                        text=str(topic_batch.get("text", "") or ""),
                        top_k=5,
                    )
                    if has_slides
                    else []
                )
                result["retrieved_pages"].update(
                    int(page.get("page_number") or 0)
                    for page in relevant_slide_pages
                    if page.get("page_number")
                )
                already_covered_digest = "\n".join(base_digest_lines)
                if sub_batch_index > 1 and topic_concept_titles:
                    earlier_parts_titles = "، ".join(topic_concept_titles)
                    already_covered_digest += (
                        ("\n" if already_covered_digest else "")
                        + f"- {topic['title']} (earlier parts of this same topic): "
                        + earlier_parts_titles
                    )
                generate_topic_batch = (
                    self._generate_taught_unit_batch_for_topic
                    if teaching_mode
                    else self._generate_content_unit_batch_for_topic
                )
                teaching_kwargs = (
                    {
                        "topic_allocation": (budget_allocation.get("topics") or {}).get(
                            str(topic["topic_id"]), {}
                        ),
                        "teaching_plan": teaching_plan,
                    }
                    if teaching_mode
                    else {}
                )
                generate_part = partial(
                    generate_topic_batch,
                    **teaching_kwargs,
                    lecture=lecture,
                    context=context,
                    provider=provider,
                    topic=topic,
                    topic_batch=topic_batch,
                    topic_index=topic_index,
                    total_topics=total_topics,
                    relevant_slide_pages=relevant_slide_pages,
                    topic_outline=topics,
                    already_covered_digest=already_covered_digest,
                    sub_batch_index=sub_batch_index,
                    total_sub_batches=total_sub_batches,
                    event_callback=part_event_callback,
                )
                part_label = f'topic "{topic["title"]}" part {sub_batch_index}'
                topic_units, topic_grounding_reports, batch_llm_results, topic_payload = (
                    self._units_with_recovery(
                        generate=generate_part,
                        batch=topic_batch,
                        relevant_slide_pages=relevant_slide_pages,
                        label=part_label,
                        fallback_title=str(topic["title"]),
                        enforce_grounding=not teaching_mode,
                    )
                )
                result["total_parts"] += 1
                if isinstance(topic_payload, dict) and topic_payload.get(self._PROVIDER_FAILED_KEY):
                    result["failed_parts"] += 1
                    result["last_error"] = str(topic_payload.get(self._PROVIDER_FAILED_KEY) or "")
                if quality_rev_on(context) and not teaching_mode:
                    (
                        topic_units,
                        topic_grounding_reports,
                        batch_llm_results,
                        topic_payload,
                    ), length_record = self._expand_short_v2_part(
                        first_result=(topic_units, topic_grounding_reports, batch_llm_results, topic_payload),
                        generate_part=generate_part,
                        topic=topic,
                        topic_batch=topic_batch,
                        relevant_slide_pages=relevant_slide_pages,
                        label=part_label,
                        chunk_texts=self._batch_chunk_texts(context=context, topic_batch=topic_batch),
                    )
                    if length_record:
                        result["thin_expansions"].append(length_record)
                if teaching_mode:
                    (
                        (
                            topic_units,
                            topic_grounding_reports,
                            batch_llm_results,
                            topic_payload,
                        ),
                        expansion_record,
                    ) = self._expand_thin_teaching_part(
                        first_result=(
                            topic_units,
                            topic_grounding_reports,
                            batch_llm_results,
                            topic_payload,
                        ),
                        generate_part=generate_part,
                        topic=topic,
                        topic_batch=topic_batch,
                        relevant_slide_pages=relevant_slide_pages,
                        part_index=sub_batch_index,
                        part_target_chars=max(
                            220,
                            int(
                                (teaching_kwargs["topic_allocation"] or {}).get("target_chars", 0)
                                or 0
                            )
                            // max(1, total_sub_batches),
                        ),
                        label=part_label,
                    )
                    if expansion_record:
                        result["thin_expansions"].append(expansion_record)
                result["grounding_reports"].extend(topic_grounding_reports)
                if teaching_mode:
                    payload_dict = topic_payload if isinstance(topic_payload, dict) else {}
                    taught_items = self._audit_list(payload_dict.get("taught"))
                    omitted_items = self._audit_list(payload_dict.get("omitted"))
                    result["selection_audits"].append(
                        {
                            "topic_id": topic.get("topic_id"),
                            "importance": topic.get("importance"),
                            "part": sub_batch_index,
                            "taught_count": len(taught_items),
                            "omitted_count": len(omitted_items),
                            "taught": taught_items[:60],
                            "omitted": omitted_items[:60],
                            "unit_count": len(topic_units),
                        }
                    )
                for unit in topic_units:
                    normalized_unit = self._normalize_content_unit(
                        unit=unit,
                        batch=topic_batch,
                        batch_index=topic_index,
                        order_in_batch=topic_order_counter,
                        relevant_slide_pages=relevant_slide_pages,
                        teaching_mode=teaching_mode,
                    )
                    topic_order_counter += 1
                    # Stage B may phrase each unit's section_title slightly
                    # differently even within one topic; forcing it to the
                    # topic's own title keeps every unit of a topic under one
                    # section-header and keeps the table-of-contents entry
                    # linkable to a real, exact-matching anchor in the body.
                    # An off-topic unit skips the section-title forcing (it
                    # never gets a section-header), but still gets the same
                    # topic_anchor so its unrelated-discussion-box is a valid
                    # link target instead of a dead, unclickable TOC entry.
                    if normalized_unit.get("content_type") != "off-topic":
                        normalized_unit["section_title"] = topic["title"]
                        normalized_unit["topic_order"] = topic_index
                    normalized_unit["topic_anchor"] = topic_anchor
                    if teaching_mode:
                        # Carried so the budget-enforcement pass can group
                        # units by topic and protect core material.
                        normalized_unit["topic_id"] = str(topic["topic_id"])
                        normalized_unit["topic_importance"] = self._normalize_topic_importance(
                            topic.get("importance")
                        )
                        normalized_unit["topic_key_concepts"] = list(topic.get("key_concepts") or [])[:6]
                    result["content_units"].append(normalized_unit)
                for unit in topic_units:
                    concept_title = str(unit.get("concept_title", "") or "").strip()
                    content_type = str(unit.get("content_type", "") or "").strip().lower()
                    if concept_title and content_type != "off-topic" and (
                        concept_title not in topic_concept_titles
                    ):
                        topic_concept_titles.append(concept_title)
                result["llm_results"].extend(batch_llm_results)
                with writing_state_lock:
                    writing_state["written_chars"] += len(str(topic_batch.get("text", "") or ""))
                emit(
                    f"Wrote part {sub_batch_index} of {total_sub_batches} of topic "
                    f"{topic_index} of {total_topics} ({topic['title']})."
                )

            # Feeds later topics' prompts so they can refer back to
            # already-established concepts instead of re-deriving them.
            if topic_concept_titles:
                joined_titles = "، ".join(topic_concept_titles)
                result["digest_line"] = f"- {topic['title']}: {joined_titles}"
            result["timing"] = {
                "topic_index": topic_index,
                "started_offset_seconds": round(topic_started_at - writing_started_at, 2),
                "seconds": round(time.monotonic() - topic_started_at, 2),
            }
            return result

        def report_progress(message: str) -> None:
            self.update(
                note=note,
                status=NoteGenerationStatus.STRUCTURING,
                progress=writing_progress(),
                stage_detail="REQUESTING_MODEL",
                stage_message=message,
            )

        topic_results: dict[int, dict[str, Any]] = {}
        if parallelism <= 1:
            for topic_index in range(1, total_topics + 1):
                topic_results[topic_index] = write_topic(
                    topic_index,
                    digest_lines_for(topic_index, topic_results),
                    report_progress,
                    event_callback,
                    None,
                )
        else:
            topic_results = self._write_topics_in_parallel(
                total_topics=total_topics,
                parallelism=parallelism,
                topic_dependencies=topic_dependencies,
                has_topic_content=lambda index: bool(sub_batches_by_topic[index]),
                digest_lines_for=digest_lines_for,
                write_topic=write_topic,
                report_progress=report_progress,
                event_callback=event_callback,
                topic_weight=topic_weight if fully_planned else None,
            )

        # Merged in topic order, so the result is the same whichever call
        # finished first.
        for topic_index in range(1, total_topics + 1):
            topic_result = topic_results.get(topic_index)
            if not topic_result:
                continue
            content_units.extend(topic_result["content_units"])
            grounding_reports.extend(topic_result["grounding_reports"])
            selection_audits.extend(topic_result["selection_audits"])
            thin_topic_expansions.extend(topic_result["thin_expansions"])
            retrieved_slide_page_numbers.update(topic_result["retrieved_pages"])
            total_parts += topic_result["total_parts"]
            failed_parts += topic_result["failed_parts"]
            if topic_result["failed_parts"]:
                last_part_error = topic_result["last_error"]
            for llm_result in topic_result["llm_results"]:
                usage = llm_result.usage
                completion_results.append(llm_result)
                aggregate_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
                aggregate_usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
                aggregate_usage["total_tokens"] += int(usage.get("total_tokens", 0))
        timings["topic_writing_seconds"] = round(time.monotonic() - writing_started_at, 2)
        timings["topics"] = [
            topic_results[index]["timing"]
            for index in range(1, total_topics + 1)
            if topic_results.get(index) and topic_results[index].get("timing")
        ]
        post_writing_started_at = time.monotonic()

        # From here on this is identical to the chunked-mode tail: merge,
        # transcript-alignment repair, missing-concept repair, definition
        # dedup, build blocks. Those passes stay as a safety net; Stage A/B
        # already did the primary topic consolidation.
        if total_parts and failed_parts == total_parts:
            # Every part failed at the provider: raw transcript pasted as a
            # "note" is not a result, so let the outer model fallback run.
            raise PipelineError(stage="NOTE_GENERATION", message=last_part_error)
        if failed_parts:
            logger.warning(
                "note_generation_parts_recovered_from_transcript failed=%s total=%s",
                failed_parts,
                total_parts,
            )
        if not content_units:
            raise PipelineError(
                stage="NOTE_GENERATION",
                message=f"{model_name} produced no usable concept units for this lecture.",
            )
        merged_units, dedup_report = self._merge_content_units(
            content_units=content_units, chronological=not teaching_mode
        )
        unit_pipeline_audit = {
            "model_output_units_before_merging": self._unit_debug_snapshot(content_units),
            "units_after_initial_merge": self._unit_debug_snapshot(merged_units),
            "units_after_transcript_repair": [],
            "units_after_missing_concept_repair": [],
            "units_after_definition_dedup": [],
            "final_semantic_blocks": [],
        }
        if teaching_mode:
            # The transcript-alignment pass rewrites units back toward the
            # lecturer's wording; skipping it is the point of this mode.
            transcript_rewrite_report = {
                "skipped_reason": "teaching_mode",
                "rewritten_unit_count": 0,
            }
            rewrite_usage: dict[str, int] = {}
        else:
            (
                merged_units,
                transcript_rewrite_report,
                rewrite_usage,
            ) = self._repair_transcript_alignment_units(
                lecture=lecture,
                note=note,
                provider=provider,
                context=context,
                merged_units=merged_units,
                event_callback=event_callback,
            )
        aggregate_usage["prompt_tokens"] += int(rewrite_usage.get("prompt_tokens", 0))
        aggregate_usage["completion_tokens"] += int(rewrite_usage.get("completion_tokens", 0))
        aggregate_usage["total_tokens"] += int(rewrite_usage.get("total_tokens", 0))
        unit_pipeline_audit["units_after_transcript_repair"] = self._unit_debug_snapshot(
            merged_units
        )
        summarized_mode = self._note_detail_mode(context=context) == "summarized"
        if has_slides and not summarized_mode and not teaching_mode:
            (
                merged_units,
                concept_coverage_report,
                repair_pass_count,
                repair_usage,
            ) = self._repair_missing_concepts(
                lecture=lecture,
                note=note,
                model_name=model_name,
                provider=provider,
                context=context,
                merged_units=merged_units,
                event_callback=event_callback,
            )
            aggregate_usage["prompt_tokens"] += int(repair_usage.get("prompt_tokens", 0))
            aggregate_usage["completion_tokens"] += int(repair_usage.get("completion_tokens", 0))
            aggregate_usage["total_tokens"] += int(repair_usage.get("total_tokens", 0))
        else:
            concept_coverage_report = {
                "inventory_size": len(context.get("concept_inventory", [])),
                "covered_concepts": [],
                "missing_concepts": [],
                "repaired_concepts": [],
                "skipped_reason": (
                    "teaching_mode"
                    if teaching_mode
                    else "summarized_mode_keeps_only_major_topics"
                    if summarized_mode
                    else "no_slides_uploaded"
                ),
            }
            repair_pass_count = 0

        unit_pipeline_audit["units_after_missing_concept_repair"] = self._unit_debug_snapshot(
            merged_units
        )
        if teaching_mode:
            # The generic definition matcher cannot tell a repeated definition
            # from a second, legitimate sentence that happens to look like one,
            # and it can drop a whole unit. Teaching mode already prevents the
            # repetition upstream (exclusive chunk ownership, the already-taught
            # digest, "never say it twice"), so the matcher only cost content.
            concept_consolidation_report = {
                "input_unit_count": len(merged_units),
                "output_unit_count": len(merged_units),
                "removed_duplicate_definition_sentence_count": 0,
                "dropped_whole_unit_count": 0,
                "skipped_reason": "teaching_mode",
            }
        else:
            merged_units, concept_consolidation_report = (
                self._remove_repeated_definitions_safely(
                    units=merged_units,
                    context=context,
                    preserve_paragraphs=False,
                )
            )
        # Every version: one home per idea. The writer often says the same thing
        # as a paragraph, then as a list, then again as an example, and nothing
        # downstream compared those fields, so all three were rendered.
        merged_units, field_redundancy_report = self._remove_redundant_unit_fields(
            units=merged_units
        )
        concept_consolidation_report["redundant_unit_fields"] = field_redundancy_report
        merged_units, repeated_statement_report = self._remove_repeated_statements(
            units=merged_units
        )
        concept_consolidation_report["repeated_statements"] = repeated_statement_report
        merged_units, junk_off_topic_dropped = self._drop_junk_off_topic_units(
            units=merged_units
        )
        concept_consolidation_report["dropped_junk_off_topic_units"] = junk_off_topic_dropped
        summary_compaction_report: dict[str, Any] = {
            "enabled": False,
            "input_unit_count": len(merged_units),
            "output_unit_count": len(merged_units),
        }
        if summarized_mode:
            merged_units, summary_compaction_report = self._compact_summarized_units(
                units=merged_units,
                context=context,
            )
        budget_enforcement_report: dict[str, Any] = {"enabled": False}
        if teaching_mode:
            # v2 never condenses or trims: every point the lecturer made stays.
            (
                merged_units,
                budget_enforcement_report,
                budget_usage,
            ) = self._enforce_teaching_length_budget(
                lecture=lecture,
                note=note,
                provider=provider,
                context=context,
                units=merged_units,
                allocation=budget_allocation,
                event_callback=event_callback,
            )
            for usage_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                aggregate_usage[usage_key] += int(budget_usage.get(usage_key, 0) or 0)
        formula_started_at = time.monotonic()
        merged_units, formula_check_report, formula_usage = self._verify_unit_formulas(
            lecture=lecture, provider=provider, units=merged_units, event_callback=event_callback
        )
        timings["formula_verification_seconds"] = round(time.monotonic() - formula_started_at, 2)
        for usage_key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            aggregate_usage[usage_key] += int(formula_usage.get(usage_key, 0) or 0)
        unit_pipeline_audit["formula_verification"] = formula_check_report
        timings["post_writing_seconds"] = round(time.monotonic() - post_writing_started_at, 2)
        unit_pipeline_audit["units_after_definition_dedup"] = self._unit_debug_snapshot(
            merged_units
        )
        blocks = self._build_semantic_blocks_from_units(
            lecture=lecture,
            context=context,
            units=merged_units,
            document_title=document_title,
        )
        labels = semantic_labels_for_language(lecture.output_language or "fa")
        unit_pipeline_audit["final_semantic_blocks"] = self._block_debug_snapshot(blocks)
        # Built from the FINAL rendered blocks, not from rendered_topic_ids
        # (set earlier, right after Stage B, before merge/repair/dedup could
        # still drop a topic's only unit entirely -- e.g. a unit whose body
        # turned out to be nothing but a duplicate definition). A topic
        # whose content got dropped that way must not keep a table-of-
        # contents link pointing at an anchor that no longer exists in the
        # body.
        rendered_anchor_ids = {
            str(block.get("anchor_id", "") or "").strip()
            for block in blocks
            if block.get("anchor_id")
        }
        off_topic_anchor_ids = {
            str(block.get("anchor_id", "") or "").strip()
            for block in blocks
            if block.get("type") == "unrelated-discussion-box" and block.get("anchor_id")
        }
        toc_block = self._build_topics_toc_block(
            topics=topics,
            labels=labels,
            rendered_anchor_ids=rendered_anchor_ids,
            excluded_anchor_ids=off_topic_anchor_ids,
            drop_unrendered=quality_rev_v42(context),
        )
        document = {
            "document_title": document_title,
            "pages": [
                {
                    "header": {
                        "course_name": lecture.course_name,
                        "course_detail": lecture.professor_name,
                        "session_number": lecture.session_number,
                        "lecture_badge": "",
                    },
                    "blocks": [toc_block, *blocks] if toc_block else blocks,
                    "footer": {
                        "page_number": f"{labels['page']} 1",
                        "watermark": "",
                    },
                }
            ],
        }
        self._validate_semantic_document(document)
        slide_reference_coverage = self._estimate_slide_reference_coverage(blocks=blocks)
        return document, aggregate_usage, {
            "batch_count": total_topics,
            "topic_outline": topics,
            "concept_inventory": context.get("concept_inventory", [])[:40],
            "concept_coverage_report": concept_coverage_report,
            "dedup_report": {
                **dedup_report,
                "transcript_rewrite_report": transcript_rewrite_report,
            },
            "concept_consolidation_report": concept_consolidation_report,
            "summary_compaction_report": summary_compaction_report,
            "note_generation_input_audit": generation_input_audit,
            "unit_grounding_report": self._summarize_unit_grounding_reports(grounding_reports),
            "unit_pipeline_audit": {
                **unit_pipeline_audit,
                "term_guard": getattr(self, "_term_guard_report", {}),
            },
            "teaching_plan": teaching_plan,
            "teaching_budget": budget_allocation,
            "teaching_selection_audits": selection_audits,
            "teaching_thin_topic_expansions": thin_topic_expansions,
            "teaching_budget_enforcement": budget_enforcement_report,
            "structure_timings": timings,
            "slide_reference_coverage": {
                **slide_reference_coverage,
                "retrieved_slide_pages": sorted(
                    number for number in retrieved_slide_page_numbers if number
                ),
            },
            "repair_pass_count": (
                repair_pass_count
                + int(transcript_rewrite_report.get("rewritten_unit_count", 0))
            ),
            "provider_call_summary": summarize_completion_results(completion_results),
        }

    @staticmethod
    def _effective_structure_parallelism(*, writable_topics: int) -> int:
        """How many topics of one note may be written at once.

        Capped by the global provider slot count minus one, so a single note can
        never hold every slot and starve other users' notes (fairness stays with
        the slot gate; this only bounds one note's share of it).
        """
        configured = int(getattr(settings, "NOTE_STRUCTURE_PARALLELISM", 1) or 1)
        slots = int(getattr(settings, "NOTE_PROVIDER_MAX_CONCURRENCY", 0) or 0)
        if slots > 0:
            configured = min(configured, max(1, slots - 1))
        return max(1, min(configured, writable_topics))

    def _write_topics_in_parallel(
        self,
        *,
        total_topics: int,
        parallelism: int,
        topic_dependencies: Any,
        has_topic_content: Any,
        digest_lines_for: Any,
        write_topic: Any,
        report_progress: Any,
        event_callback: Any | None,
        topic_weight: Any | None = None,
    ) -> dict[int, dict[str, Any]]:
        """Write topics with a sliding window of ``parallelism`` in flight.

        With ``topic_weight`` every topic's "already covered" list comes from the plan, so no
        topic waits for another: the heaviest topics start first and a free worker always
        takes the next one (the result is merged in topic order either way).

        Topic ``k`` starts once every topic up to ``k - parallelism`` (and every
        topic it explicitly refers back to) has finished, so the "already
        covered" digest it receives depends on position only, never on timing.
        Workers do no database work: progress messages and provider events are
        queued and applied here, on the calling thread.
        """
        messages: queue.SimpleQueue = queue.SimpleQueue()
        abort = threading.Event()
        results: dict[int, dict[str, Any]] = {}

        def queued_emit(message: str) -> None:
            messages.put(("message", message))

        def queued_event_callback(event: dict[str, Any]) -> None:
            # Per-call "attempt"/"slot acquired" chatter from several topics at
            # once would only overwrite the status line; waits and failures stay.
            if str(event.get("event", "")) in {"provider_attempt", "provider_slot_acquired"}:
                return
            messages.put(("event", event))

        def drain_messages() -> None:
            while True:
                try:
                    kind, payload = messages.get_nowait()
                except queue.Empty:
                    return
                try:
                    if kind == "message":
                        report_progress(payload)
                    elif event_callback is not None:
                        event_callback(payload)
                except Exception:
                    logger.exception("note_generation_progress_update_failed")

        def worker(topic_index: int, digest_lines: list[str]) -> dict[str, Any]:
            try:
                return write_topic(
                    topic_index,
                    digest_lines,
                    queued_emit,
                    queued_event_callback if event_callback is not None else None,
                    abort,
                )
            except BaseException:
                abort.set()
                raise
            finally:
                connections.close_all()

        executor = ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="note-topic")
        in_flight: dict[Future, int] = {}
        failures: dict[int, BaseException] = {}
        next_index = 1
        pending_order: list[int] = []
        unordered = topic_weight is not None
        if unordered:
            for index in range(1, total_topics + 1):
                if has_topic_content(index):
                    pending_order.append(index)
                else:
                    report_progress(f"Skipping topic {index} of {total_topics}: nothing to write.")
                    results[index] = {
                        "content_units": [],
                        "grounding_reports": [],
                        "selection_audits": [],
                        "thin_expansions": [],
                        "llm_results": [],
                        "retrieved_pages": set(),
                        "total_parts": 0,
                        "failed_parts": 0,
                        "last_error": "",
                        "digest_line": "",
                    }
            pending_order.sort(key=lambda index: (-float(topic_weight(index)), index))
            next_index = total_topics + 1
        try:
            while next_index <= total_topics or pending_order or in_flight:
                while unordered and pending_order and len(in_flight) < parallelism and not failures:
                    index = pending_order.pop(0)
                    future = executor.submit(
                        copy_context().run, worker, index, digest_lines_for(index, results)
                    )
                    in_flight[future] = index
                while next_index <= total_topics and not failures:
                    window_done = all(
                        index in results for index in range(1, max(1, next_index - parallelism + 1))
                    )
                    if not (window_done and all(dep in results for dep in topic_dependencies(next_index))):
                        break
                    if not has_topic_content(next_index):
                        report_progress(
                            f"Skipping topic {next_index} of {total_topics}: nothing to write."
                        )
                        results[next_index] = {
                            "content_units": [],
                            "grounding_reports": [],
                            "selection_audits": [],
                            "thin_expansions": [],
                            "llm_results": [],
                            "retrieved_pages": set(),
                            "total_parts": 0,
                            "failed_parts": 0,
                            "last_error": "",
                            "digest_line": "",
                        }
                        next_index += 1
                        continue
                    future = executor.submit(
                        copy_context().run,
                        worker,
                        next_index,
                        digest_lines_for(next_index, results),
                    )
                    in_flight[future] = next_index
                    next_index += 1
                if not in_flight:
                    if failures:
                        break
                    raise PipelineError(
                        stage="NOTE_GENERATION",
                        message="Topic scheduling stalled before every topic was written.",
                    )
                finished, _ = wait(list(in_flight), timeout=1.0, return_when=FIRST_COMPLETED)
                drain_messages()
                for future in finished:
                    topic_index = in_flight.pop(future)
                    try:
                        results[topic_index] = future.result()
                    except BaseException as exc:  # noqa: BLE001 - re-raised below
                        failures[topic_index] = exc
                if failures:
                    abort.set()
        finally:
            abort.set()
            # Waiting (instead of abandoning running calls) keeps a failed note
            # from leaving model calls alive next to the fallback attempt.
            executor.shutdown(wait=True, cancel_futures=True)
            drain_messages()
        real_failures = {
            index: exc for index, exc in failures.items() if not isinstance(exc, _TopicWritingAborted)
        }
        if real_failures:
            raise real_failures[min(real_failures)]
        if failures:
            raise PipelineError(stage="NOTE_GENERATION", message="Topic writing was aborted.")
        return results

    def _generate_topic_outline(
        self,
        *,
        lecture: Lecture,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        event_callback: Any | None = None,
        teaching_mode: bool = False,
    ) -> tuple[dict[str, Any], dict[str, int], Any]:
        system_prompt = (
            "You are a source-grounded lecture-note editor planning the structure of a note before "
            "writing it. "
            "You are given the ENTIRE lecture transcript at once, specifically so you can see the "
            "whole session before deciding on structure. "
            "Your only job here is to identify the distinct topics taught and group them -- do not "
            "write note content yet. "
            f"{self._language_instruction(context=context)} "
            "A professor commonly returns to the same topic more than once during a lecture "
            "(introduces it, moves on to something else, then comes back to it later with more "
            "detail, a related example, or a correction). You MUST recognize every such return "
            "and group ALL of it under one topic, identified once, instead of creating a "
            "separate topic each time it is mentioned. "
            "Order topics in the order the professor first taught them, so the note follows "
            "the lecture as it was given. "
            "Every part of the transcript that teaches something must belong to at least one "
            "topic; never silently drop taught content. "
            "Pure chatter -- jokes, greetings, small talk, personal anecdotes, classroom or "
            "equipment problems -- belongs to no topic and is simply left out. Anything "
            "that could matter for studying (assessment, deadlines, grading, what will be on "
            "the exam, assigned work) is course material and stays. If a short portion is "
            "genuinely unrelated to the course yet coherent and worth a student's glance, put "
            "it under one single catch-all topic for unrelated discussion rather than inventing "
            "a course topic for it. "
            "Keep the topic count reasonable (usually well under ten for a single lecture): "
            "group related sub-points under one broader topic rather than splitting the "
            "lecture into many narrow, thinly-populated topics, since each topic becomes its "
            "own labeled section in the final note and too many sections reads as cluttered "
            "rather than a clear book-like structure. "
            "A foundational distinction or concept that the professor relies on across "
            "several later topics (e.g. a core definition or comparison used repeatedly) "
            "belongs to exactly one topic -- normally wherever it is first properly taught -- "
            "even if later topics refer back to it; do not let the same foundational material "
            "resurface as its own repeated topic. "
            "Return JSON only."
        )
        if teaching_mode:
            system_prompt = self._teaching_outline_system_prompt(context=context)
        chunk_index_list = [
            {
                "chunk_index": int(chunk.get("chunk_index", 0)),
                "preview": str(chunk.get("text", "") or "")[:80],
            }
            for chunk in context.get("transcript_chunks", [])
        ]
        user_prompt = (
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"Course hints: {json.dumps(context['course_hints'], ensure_ascii=False)}\n"
            f"Slide context:\n{self._slide_context_snippet(context=context)}\n"
            + (
                "Return JSON with keys, in this order: lecture_thesis, learning_objectives, "
                "document_title, topics, omitted\n"
                if teaching_mode
                else "Return JSON with keys: document_title, topics\n"
            )
            + (
                "- lecture_thesis: ONE sentence saying what this session was actually about. "
                "Write this first; it orients the plan\n"
                "- learning_objectives: 3 to 7 short strings, each something the student must "
                "be able to DO after reading the note (state, derive, compare, apply, "
                "recognise...). "
                + (
                    "These are the standard for what is worth teaching\n"
                    if self._note_detail_mode(context=context) == "summarized"
                    else "They name the main outcomes; they are NOT a filter. Neither the "
                    "thesis nor the objectives decide whether something the lecturer taught "
                    "is kept: a subject the lecturer taught as course material (a related "
                    "technique, a classification, a side family of things, a diagnostic or "
                    "practical method) still gets its own topic even when it sits outside "
                    "the thesis\n"
                )
                if teaching_mode
                else ""
            )
            + "- document_title: a short, descriptive title for the lecture as a whole, naming "
            "the main subject actually taught (e.g. the central topic or technique), in the "
            "selected output language. Never a single narrow sub-topic from partway through "
            "the lecture, and never an administrative/off-topic/catch-all topic's own title\n"
            "- topics must be an array, ordered the way they should appear in the final note\n"
            + (
                "- each topic is an object with keys: topic_id, title, scope, importance, "
                "objective_ids, refers_back_to"
                + (
                    ", key_concepts"
                    if getattr(settings, "NOTE_TOPIC_PLAN_DIGEST_ENABLED", False)
                    else ""
                )
                + ", transcript_chunk_indexes\n"
                if teaching_mode
                else "- each topic is an object with keys: topic_id, title, scope, "
                "transcript_chunk_indexes\n"
            )
            + "- topic_id: a short url-safe slug in English (lowercase letters, digits, hyphens "
            "only), unique within this response\n"
            f"- title: the topic's heading. {self._language_instruction(context=context)} "
            "When the lecturer named the topic with an English term, write the heading in the "
            "output language with that English term in parentheses, e.g. «بیش‌برازش (overfitting)»\n"
            "- scope: one short sentence describing what should be covered under this topic\n"
            + (
                "- importance: exactly one of core, supporting, mention, off_topic. core = a "
                "concept, technique, rule, case or procedure the lecturer actually taught -- "
                "this is the right tier for most course topics; supporting = course material "
                "the lecturer covered more briefly or as background to a core topic, still "
                "taught properly; mention = a term the lecturer only named in passing without "
                "defining or explaining it (use it rarely, and never for anything that was "
                "defined, explained, exemplified or flagged for the exam); off_topic = the "
                "single catch-all topic for non-course remarks\n"
                "- objective_ids: array of 0-based indexes into learning_objectives that this "
                "topic serves; empty for mention and off_topic\n"
                "- refers_back_to: array of topic_id values this topic depends on and should "
                "refer back to rather than re-teach; empty when it depends on nothing\n"
                + (
                    "- key_concepts: array of 3 to 8 short concept names (a few words each, in "
                    "the output language) that this topic will define or teach for the first "
                    "time. Later topics are written from this list without seeing this "
                    "topic's text, so name the concepts precisely and only those this topic "
                    "itself teaches\n"
                    if getattr(settings, "NOTE_TOPIC_PLAN_DIGEST_ENABLED", False)
                    else ""
                )
                if teaching_mode
                else ""
            )
            + "- transcript_chunk_indexes: array of integers -- EVERY chunk_index below where this "
            "topic is discussed, even when the indexes are not contiguous"
            + (
                ". Each chunk_index must appear under EXACTLY ONE topic; if two topics touch "
                "the same chunk, give it to the one that actually teaches its material and "
                "let the other refer back"
                if teaching_mode
                else ""
            )
            + "\n"
            + (
                "- omitted: array of short strings for the NOISE you are leaving out, each "
                "with a brief reason (e.g. \"repeated the same definition three times -- "
                "taught once\", \"greeting and attendance remarks\"). Only noise belongs "
                "here: never a concept, definition, rule, formula, example, case, warning or "
                "exam hint. An empty list is correct when the lecture has no noise worth "
                "listing\n"
                if teaching_mode
                else ""
            )
            +
            "Transcript chunk indexes available (use the preview only to judge topic boundaries, "
            "the preview is not the full content of that chunk):\n"
            f"{json.dumps(chunk_index_list, ensure_ascii=False)}\n"
            f"Full transcript text:\n{context['transcript_text']}"
        )
        if not teaching_mode:
            system_prompt, user_prompt = apply_v2_planner_rules(system_prompt, user_prompt, context)
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        else:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        return payload, llm_result.usage, llm_result

    def _slugify_topic_id(self, raw: str, *, existing: set[str]) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", str(raw or "").strip().lower()).strip("-")
        slug = slug or "topic"
        candidate = slug
        suffix = 2
        while candidate in existing:
            candidate = f"{slug}-{suffix}"
            suffix += 1
        return candidate

    def _normalize_topic_outline(
        self,
        *,
        payload: dict[str, Any],
        context: dict[str, Any],
        lecture: Lecture,
        teaching_mode: bool = False,
    ) -> list[dict[str, Any]]:
        raw_topics = payload.get("topics") if isinstance(payload, dict) else None
        all_chunk_indexes = {
            int(chunk.get("chunk_index", 0)) for chunk in context.get("transcript_chunks", [])
        }
        topics: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        covered_chunk_indexes: set[int] = set()

        if isinstance(raw_topics, list):
            for raw_topic in raw_topics:
                if not isinstance(raw_topic, dict):
                    continue
                title = str(raw_topic.get("title", "") or "").strip()
                if not title:
                    continue
                topic_id = self._slugify_topic_id(
                    str(raw_topic.get("topic_id", "") or title), existing=seen_ids
                )
                seen_ids.add(topic_id)
                chunk_indexes = sorted(
                    {
                        int(idx)
                        for idx in (raw_topic.get("transcript_chunk_indexes") or [])
                        if str(idx).strip().lstrip("-").isdigit() and int(idx) in all_chunk_indexes
                    }
                )
                if teaching_mode:
                    # Exclusive ownership: a chunk taught by two topics is
                    # taught twice, and teaching mode has no dedup pass that
                    # could catch it afterwards. First claim in planned order
                    # wins, and the losing topic refers back instead.
                    chunk_indexes = [
                        idx for idx in chunk_indexes if idx not in covered_chunk_indexes
                    ]
                covered_chunk_indexes.update(chunk_indexes)
                topic: dict[str, Any] = {
                    "topic_id": topic_id,
                    "title": title,
                    "scope": str(raw_topic.get("scope", "") or "").strip(),
                    "order": len(topics),
                    "transcript_chunk_indexes": chunk_indexes,
                }
                if teaching_mode:
                    topic.update(
                        {
                            "importance": self._normalize_topic_importance(
                                raw_topic.get("importance")
                            ),
                            "objective_ids": [
                                int(value)
                                for value in (raw_topic.get("objective_ids") or [])
                                if str(value).strip().isdigit()
                            ],
                            "refers_back_to": [
                                str(value).strip()
                                for value in (raw_topic.get("refers_back_to") or [])
                                if str(value).strip()
                            ],
                        }
                    )
                    key_concepts = self._planned_key_concepts(raw_topic.get("key_concepts"))
                    if key_concepts:
                        topic["key_concepts"] = key_concepts
                topics.append(topic)

        if teaching_mode:
            # Exclusive ownership can leave a topic with nothing of its own.
            # Such a topic has no material to teach, so writing it would only
            # restate a neighbour; its dependants still refer back to it by id.
            topics = [topic for topic in topics if topic["transcript_chunk_indexes"]]
            # The note follows the lecturer's sequence: a topic sits where its
            # earliest chunk was spoken, whatever order the planner listed it
            # in. The catch-all for non-course remarks stays last.
            topics.sort(
                key=lambda topic: (
                    topic.get("importance") == "off_topic",
                    min(topic["transcript_chunk_indexes"]),
                )
            )
            for order, topic in enumerate(topics):
                topic["order"] = order

        # Coverage guarantee: every transcript chunk must belong to a topic.
        # A professor's content never gets silently dropped just because the
        # structuring pass missed it.
        missing_chunk_indexes = sorted(all_chunk_indexes - covered_chunk_indexes)
        if missing_chunk_indexes:
            labels = semantic_labels_for_language(lecture.output_language or "fa")
            fallback_topic: dict[str, Any] = {
                "topic_id": self._slugify_topic_id("uncovered-content", existing=seen_ids),
                "title": labels["other_topics_title"],
                "scope": "",
                "order": len(topics),
                "transcript_chunk_indexes": missing_chunk_indexes,
            }
            if teaching_mode:
                # Unplanned leftovers are not what the lecture was about, so
                # they get a supporting budget rather than a core one.
                fallback_topic.update(
                    {"importance": "supporting", "objective_ids": [], "refers_back_to": []}
                )
            topics.append(fallback_topic)

        if not topics and all_chunk_indexes:
            # Total structuring failure: fall back to one topic covering the
            # whole transcript so the lecture can still produce a note.
            whole_lecture_topic: dict[str, Any] = {
                "topic_id": "lecture-content",
                "title": str(context["lecture"].get("title") or lecture.title),
                "scope": "",
                "order": 0,
                "transcript_chunk_indexes": sorted(all_chunk_indexes),
            }
            if teaching_mode:
                whole_lecture_topic.update(
                    {"importance": "core", "objective_ids": [], "refers_back_to": []}
                )
            topics = [whole_lecture_topic]
        return topics

    @staticmethod
    def _planned_key_concepts(value: Any) -> list[str]:
        """The planner's concept names for a topic: short, unique, at most 8."""
        if not isinstance(value, list):
            return []
        concepts: list[str] = []
        for item in value:
            name = " ".join(str(item or "").split())[:80]
            if name and name not in concepts:
                concepts.append(name)
        return concepts[:8]

    @staticmethod
    def _audit_list(value: Any) -> list[str]:
        """Non-empty strings from a model-supplied audit field, in order."""
        return [str(item).strip() for item in (value or []) if str(item).strip()]

    @staticmethod
    def _normalize_topic_importance(value: Any) -> str:
        """Map the planner's importance tier onto the four tiers we budget for.

        An unrecognized or missing tier becomes "supporting": neither the full
        core treatment nor a one-liner, so a planner that omits the field
        degrades into a reasonable middle rather than the old teach-everything
        behaviour.
        """
        tier = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        if tier in {"core", "supporting", "mention", "off_topic"}:
            return tier
        if tier in {"offtopic", "off_curriculum", "unrelated"}:
            return "off_topic"
        return "supporting"

    def _topic_pseudo_batch(
        self, *, context: dict[str, Any], topic: dict[str, Any], topic_index: int
    ) -> dict[str, Any]:
        chunks_by_index = {
            int(chunk.get("chunk_index", 0)): chunk
            for chunk in context.get("transcript_chunks", [])
        }
        selected_chunks = [
            chunks_by_index[idx]
            for idx in topic["transcript_chunk_indexes"]
            if idx in chunks_by_index
        ]
        text = "\n\n".join(
            str(chunk.get("text", "") or "").strip()
            for chunk in selected_chunks
            if str(chunk.get("text", "") or "").strip()
        )
        start_values = [float(chunk.get("start_ts", 0.0) or 0.0) for chunk in selected_chunks]
        end_values = [float(chunk.get("end_ts", 0.0) or 0.0) for chunk in selected_chunks]
        return {
            "batch_index": topic_index,
            "chunk_indexes": list(topic["transcript_chunk_indexes"]),
            "start_ts": min(start_values) if start_values else 0.0,
            "end_ts": max(end_values) if end_values else 0.0,
            "text": text,
        }

    def _split_topic_into_sub_batches(
        self,
        *,
        context: dict[str, Any],
        topic: dict[str, Any],
        topic_index: int,
        teaching_mode: bool = False,
    ) -> list[dict[str, Any]]:
        # A single Stage B call per topic ("usually one unit") works for a
        # short topic, but a topic spanning a large share of the lecture
        # (e.g. nearly half the transcript) was still being compressed into
        # roughly the same one or two units -- the model had no reason to
        # write proportionally more just because it was given more source
        # text. Splitting a large topic into char-bounded sub-batches (the
        # same size v1's chunked pipeline uses per call) gives the model one
        # writing pass per natural-sized portion of the source material
        # instead of one pass for the whole topic, so note length scales
        # with how much the professor actually said, not with topic count.
        chunks_by_index = {
            int(chunk.get("chunk_index", 0)): chunk
            for chunk in context.get("transcript_chunks", [])
        }
        selected_chunks = [
            chunks_by_index[idx]
            for idx in topic["transcript_chunk_indexes"]
            if idx in chunks_by_index
        ]
        char_limit = self._semantic_batch_char_limit(context=context)
        if teaching_mode:
            # Teaching mode writes a selection, not an expansion, so it does
            # not need a smaller slice. A smaller slice would be actively
            # harmful here: each extra part is another call that pays the full
            # teaching-arc overhead and adds another chance to repeat itself.
            char_limit = max(char_limit, 6000)
        chunk_groups: list[list[dict[str, Any]]] = []
        current_group: list[dict[str, Any]] = []
        current_chars = 0
        for chunk in selected_chunks:
            chunk_text = str(chunk.get("text", "") or "").strip()
            if not chunk_text:
                continue
            if current_group and current_chars + len(chunk_text) > char_limit:
                chunk_groups.append(current_group)
                current_group = []
                current_chars = 0
            current_group.append(chunk)
            current_chars += len(chunk_text)
        if current_group:
            chunk_groups.append(current_group)

        sub_batches: list[dict[str, Any]] = []
        for group in chunk_groups:
            text = "\n\n".join(
                str(chunk.get("text", "") or "").strip()
                for chunk in group
                if str(chunk.get("text", "") or "").strip()
            )
            start_values = [float(chunk.get("start_ts", 0.0) or 0.0) for chunk in group]
            end_values = [float(chunk.get("end_ts", 0.0) or 0.0) for chunk in group]
            sub_batches.append(
                {
                    "batch_index": topic_index,
                    "chunk_indexes": [int(chunk.get("chunk_index", 0)) for chunk in group],
                    "start_ts": min(start_values) if start_values else 0.0,
                    "end_ts": max(end_values) if end_values else 0.0,
                    "text": text,
                }
            )
        return sub_batches

    def _build_topics_toc_block(
        self,
        *,
        topics: list[dict[str, Any]],
        labels: dict[str, str],
        rendered_anchor_ids: set[str],
        excluded_anchor_ids: set[str] | None = None,
        drop_unrendered: bool = False,
    ) -> dict[str, Any] | None:
        items = []
        for topic in topics:
            title = str(topic.get("title", "") or "").strip()
            if not title:
                continue
            topic_id = str(topic.get("topic_id") or "")
            candidate_anchor = f"topic-{topic_id}"
            if candidate_anchor in (excluded_anchor_ids or set()):
                continue
            anchor = candidate_anchor if candidate_anchor in rendered_anchor_ids else ""
            if drop_unrendered and not anchor:
                # Revision 3: a contents line for a section that is not in the note is a dead entry.
                continue
            items.append({"title": title, "anchor": anchor})
        if len(items) < 2:
            return None
        return {
            "type": "table-of-contents",
            "title": labels.get("topics_covered_title", labels["summary_title"]),
            "items": items,
        }

    def _generate_content_unit_batch_for_topic(
        self,
        *,
        lecture: Lecture,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        topic: dict[str, Any],
        topic_batch: dict[str, Any],
        topic_index: int,
        total_topics: int,
        relevant_slide_pages: list[dict[str, Any]],
        topic_outline: list[dict[str, Any]],
        already_covered_digest: str,
        sub_batch_index: int = 1,
        total_sub_batches: int = 1,
        retry_attempt: int = 0,
        event_callback: Any | None = None,
        expand_hint: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], Any]:
        is_split_topic = total_sub_batches > 1
        split_topic_instruction = (
            (
                f"This topic's source material was long enough to be split into "
                f"{total_sub_batches} parts so you have room to cover it thoroughly "
                f"instead of compressing it; you are writing part {sub_batch_index} of "
                f"{total_sub_batches}. The excerpts below are ONLY this part's portion, "
                "in chronological order -- do not try to recap the whole topic, and do "
                "not repeat anything an earlier part of this same topic already covered "
                "(the digest below will show that). "
            )
            if is_split_topic
            else ""
        )
        system_prompt = (
            "You are a source-grounded lecture-note editor. "
            "You are writing the note content for ONE topic of a lecture. The transcript "
            "excerpts given to you below are every part of the lecture where this topic was "
            "discussed, already gathered together even if the professor covered it at "
            "different, non-contiguous points in the session. "
            f"{self._language_instruction(context=context)} "
            f"{self._detail_mode_instruction(context=context)} "
            "Do NOT add external textbook definitions, examples, explanations, or prior "
            "knowledge. "
            "Do NOT define a term unless the professor explicitly defined or explained it in "
            "these excerpts, or the provided slide pages directly state that definition. "
            "If a term is only mentioned, mention it briefly without inventing a definition. "
            f"{self._source_selection_instruction(context=context)} "
            "Transcript is the primary source of truth. Use slides only to disambiguate "
            "unclear transcript wording, recover technical terms, complete incomplete "
            "phrases, and attach correct slide grounding. "
            "The transcript is machine-generated speech-to-text and can mishear a technical "
            "term as an unrelated ordinary word, especially short notation spoken aloud (e.g. "
            "complexity notation like O(n), O(n^2), O(log n), Omega, Theta can be misheard as "
            "an ordinary Persian word that does not fit the sentence). "
            "When a transcript word does not make sense in its technical context and the "
            "surrounding transcript or slide evidence clearly implies the intended term, "
            "silently write the correct term instead of the misheard word. "
            "Never wrap a suspected mis-transcription in quotation marks as if it were the "
            "professor's intended term; either correct it to the best-supported term or, if "
            "the term cannot be recovered, keep the professor's point and describe it in "
            "plain words instead of quoting nonsense. "
            f"{SPEECH_TO_TEXT_CORRECTION_PROMPT}"
            f"{LECTURER_WORDS_PROMPT}"
            f"{ONE_HOME_PROMPT}"
            f"{FAITHFUL_NOTE_PROMPT}"
            f"{quality_rev_prompt(context)}"
            "Since you are given every mention of this topic together, write ONE coherent, "
            "complete treatment of it -- merge the separate mentions into flowing prose "
            "instead of repeating the definition or restating the same point once per "
            "mention. "
            f"{split_topic_instruction}"
            "Cover EVERYTHING the professor taught in these excerpts, with as many concept "
            "units as that requires: a student will study for an exam from this note alone, "
            "so a missing point is a failure. Never shorten the note by dropping a point; keep "
            "it readable by writing each point tightly and removing only spoken repetition, "
            "filler and restarts. "
            "For a worked example, read the full excerpt as one sequence and combine its "
            "setup, board references, substitutions, intermediate steps, and conclusion. "
            "Reconstruct omitted board writing only when the result is logically determined "
            "by transcript or slide evidence; otherwise state only what the professor said "
            "and do not invent values or steps. "
            "Never let slides replace the transcript. Never generate a concept only because "
            "it exists on slides unless these excerpts clearly mention it or strongly imply "
            "it. "
            "You are given the full topic outline for this note and a digest of what earlier "
            "topics already covered (both below). This topic's scope may legitimately rely on "
            "a foundational distinction or definition that one of those earlier topics already "
            "established. If so, refer back to it in one brief phrase (e.g. naming the earlier "
            "topic) instead of re-explaining or redefining it in full here -- the full "
            "treatment belongs only to its own topic, and restating it reads as cluttered, "
            "redundant content to a student reading the note start to finish. "
            "Only populate note or example when it genuinely adds information the body does "
            "not already state; never manufacture a note or example merely to fill the field, "
            "since an empty field produces a cleaner note than a token-filler box. "
            "An example must earn its place with something concrete the body does not already "
            "give -- specific numbers, a traced sequence of steps, or a distinct scenario. If "
            "writing the example would mean re-explaining the same point in body in different "
            "words without adding any such concrete detail, leave example empty instead; a "
            "student should never read the same explanation twice in one concept. "
            "All output must be written in the selected output language. "
            f"{TERM_PRESERVATION_PROMPT}"
            f"{self._removal_scope_instruction(context=context)} "
            "Return JSON only."
        )
        system_prompt = apply_v2_writer_rules(system_prompt, context)
        outline_preview = json.dumps(
            [
                {"title": other["title"], "scope": other.get("scope", "")}
                for other in topic_outline
            ],
            ensure_ascii=False,
        )
        covered_digest = (
            already_covered_digest.strip() or "(nothing written yet -- this is the first topic)"
        )
        user_prompt = (
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"Course hints: {json.dumps(context['course_hints'], ensure_ascii=False)}\n"
            "Relevant slide pages for this topic:\n"
            f"{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            f"{self._lecture_terminology_hint(context=context)}"
            f"{self._spoken_english_terms_hint(context=context, text=str(topic_batch.get('text', '') or ''))}"
            f"{self._lecturer_cues_hint(context=context, text=str(topic_batch.get('text', '') or ''), topic_batch=topic_batch)}"
            f"Full topic outline for this note, in final order: {outline_preview}\n"
            "Already written in earlier topics of this same note (concept titles only -- do "
            f"not redefine these in full, refer back briefly if relevant):\n{covered_digest}\n"
            f"You are writing topic {topic_index} of {total_topics}: \"{topic['title']}\""
            + (
                f" (part {sub_batch_index} of {total_sub_batches} of this topic's material)"
                if is_split_topic
                else ""
            )
            + ".\n"
            f"Intended scope: {topic['scope']}\n"
            + "Return JSON with keys: document_title, units\n"
            "- units must be an array of concept units, in the order the professor said them; "
            "use as many as needed to cover everything substantive in these excerpts. Do not "
            "compress multiple distinct points into one unit, and do not omit real content "
            "merely to minimize the unit count\n"
            "- each unit must be an object with keys: concept_title, section_title, "
            "content_type, body, formulas, code_blocks, tables, list_items, note, example, "
            + ("example_ai, " if quality_rev_on(context) else "")
            + "slide_pages\n"
            "- content_type must be one of: definition, explanation, comparison, process, "
            "list, warning, example, figure, table, formula, summary, off-topic\n"
            + (
                "- example_ai: true only when the example is one you rebuilt because its numbers "
                "could not be recovered from the excerpts; false otherwise\n"
                if quality_rev_on(context)
                else ""
            )
            + f"{FORMULA_FIELD_PROMPT}"
            "- list_items must be plain strings only, never objects\n"
            "- body, note, and example must each be a single plain prose string, never a "
            "JSON object or array. Write a worked example as flowing sentences (state the "
            "problem, walk through the steps, give the result in prose), not as a structured "
            "object with sub-fields such as problem/setup/steps/result\n"
            f"- {self._language_instruction(context=context)}\n"
            f"- {self._detail_mode_instruction(context=context)}\n"
            "- Every text field must use the selected output language. Do not dump "
            "untranslated slide bullets unless the output language requires them\n"
            f"- {TERM_PRESERVATION_PROMPT}\n"
            "- NEVER output raw transcript formatting or speaker labels\n"
            "- Build the content from the transcript excerpts first. Use slides only to fix "
            "unclear words, restore terminology, and attach references\n"
            "- Do not import whole slide sections that are not clearly supported by these "
            "excerpts\n"
            "- Do not add definitions, explanations, examples, caveats, or applications that "
            "are not present in these excerpts or relevant slide pages\n"
            "- If a term is mentioned without explanation, keep it brief and do not expand "
            "it into a textbook explanation\n"
            "- Use content_type off-topic only for a coherent casual, personal, or "
            "administrative discussion unrelated to course learning; write it in one or "
            "two sentences in body (pure chatter such as jokes or greetings is left out "
            "entirely), leave note/example/list_items empty, and do not "
            "attach slide_pages. Off-topic content is never tied to a slide, so never write "
            "a [[slide:N]] marker anywhere in its body text either\n"
            "- If a concept has an example or caution, attach it in example or note instead "
            "of repeating the concept in another unit\n"
            "- An example field must be self-contained: include the problem/setup plus the "
            "supported steps and result, not a dangling phrase copied from speech\n"
            "- Each concept must be described once, in whichever single field fits it best "
            "(body, a list_items point, or example) -- never explain the same point in body "
            "and then again, reworded, in example or note. Example only adds a concrete "
            "instance (specific numbers, a traced sequence) that body did not already give\n"
            "- If the professor points to unseen board content, use nearby speech and slide "
            "OCR/formulas together. Never fabricate missing numbers; use variables and label "
            "a generalized reconstruction when exact values are absent\n"
            "- If the source contains enumerations or categories, put them into list_items "
            "in the same unit instead of scattering them across prose\n"
            "- Use slide_pages only from the relevant slide pages provided above\n"
            "- Attach slide pages only when the related slide is genuinely clear from the "
            "transcript and support context. If unsure, leave slide_pages empty\n"
            "- Keep the transcript's own wording whenever it is clear: copy the lecturer's "
            "sentences and only clean them, never reword them. Use slide wording only to repair "
            "unclear or incomplete transcript text\n"
            + (
                f"Transcript excerpts for THIS PART ({sub_batch_index} of "
                f"{total_sub_batches}) of the topic:\n{topic_batch['text']}"
                if is_split_topic
                else f"Transcript excerpts for this topic:\n{topic_batch['text']}"
            )
        )
        user_prompt = self._retry_prompt_prefix(retry_attempt) + user_prompt
        if expand_hint:
            hint_text = ""
            if expand_hint.get("source_words"):
                hint_text += v2_length_hint_text(
                    source_words=int(expand_hint.get("source_words", 0) or 0),
                    written_words=int(expand_hint.get("written_words", 0) or 0),
                )
            if expand_hint.get("missing_passages"):
                hint_text += v2_missing_passages_hint_text(list(expand_hint["missing_passages"]))
            user_prompt = hint_text + user_prompt
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        else:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        return payload, llm_result

    def _teaching_outline_system_prompt(self, *, context: dict[str, Any]) -> str:
        prompt = self._teaching_outline_system_prompt_base(context=context)
        return apply_planner_shape_rules(apply_story_rule(prompt, context), context)

    def _teaching_outline_system_prompt_base(self, *, context: dict[str, Any]) -> str:
        budget = self._teaching_length_budget(context=context)
        summarized = budget["detail_mode"] == "summarized"
        if summarized:
            selection_paragraph = (
                "SELECTION IS YOUR MAIN JOB. This is a condensed note: decide what the session "
                "was actually about, keep the major taught topics and the essential definitions "
                "and formulas, and let everything minor go. "
                f"The whole note must fit in about {budget['target_chars']} characters "
                f"(roughly {budget['target_words']} words), for a transcript of "
                f"{budget['source_chars']} characters -- on the order of "
                f"{budget['ratio_percent']} percent of the source. Plan a note that fits. "
            )
            topic_count_paragraph = (
                "Keep the topic count sensible for one lecture (typically 4 to 8): each topic "
                "becomes a labeled section and is written in its own pass, so merge thin "
                "neighboring ideas and split only genuinely distinct subjects. "
            )
        else:
            selection_paragraph = (
                "THE STUDENT HAS ONLY YOUR NOTE. Anything the lecturer taught that your plan "
                "leaves out is something the student cannot learn and may be examined on, so "
                "completeness of the teaching is your main job. Treat as noise, and leave out, "
                "ONLY: greetings, class logistics, jokes and personal stories, filler, restarted "
                "sentences, and the lecturer repeating a point that was already made. EVERYTHING "
                "ELSE the lecturer taught is course material and must have a place in the plan: "
                "every concept, definition, rule, formula, step, procedure, named example, "
                "comparison, case, warning, tip and exam hint. Never merge two distinct concepts "
                "into one topic just to keep the plan short, and never downgrade material to a "
                "passing mention because the lecturer said it quickly: a brief remark that states "
                "a definition, a rule, a criterion or an exam point is still taught material. "
                f"The finished note should come out at about {budget['target_chars']} characters "
                f"(roughly {budget['target_words']} words) for a transcript of "
                f"{budget['source_chars']} characters -- on the order of "
                f"{budget['ratio_percent']} percent of the source. That is a size to REACH, not "
                "a limit to hide under: a note far below it has almost certainly dropped taught "
                "material, which is as much a failure as running long. Plan topics so that the "
                "material really fills that size. "
            )
            topic_count_paragraph = (
                "Plan one topic for each distinct subject, concept family, technique or case the "
                "lecturer taught; a full lecture hour typically has 8 to 16 topics, and a plan "
                "with only a handful almost certainly merged different subjects. Orientation "
                "material -- what the subject or family of things is, how it is classified and "
                "named, synonyms and terminology, the lecturer's roadmap -- is course material "
                "and gets its own opening topic when the lecturer spent real time on it. "
                "Quantities, schedules, classifications and enumerations the lecturer gave "
                "(types, grades, intervals, percentages, doses, counts) belong to the topic "
                "they describe and are part of its scope. Merge topics "
                "only when they are the same subject the lecturer came back to, never when they "
                "are different subjects that happen to sit next to each other. Each topic becomes "
                "a labeled section written in its own pass. "
            )
        return (
            "You are an outstanding university professor designing how to teach one lecture's "
            "material to a student who missed the class. "
            "You are given the ENTIRE raw lecture transcript at once (machine-generated "
            "speech-to-text of the lecturer, in the lecturer's order, with their digressions, "
            "repetitions and returns to earlier points). The student will have only the note you "
            "are planning, so plan it the way you would structure a chapter you wrote yourself. "
            "Your job here is to decide WHAT IS TAUGHT, how deeply, and in what order "
            "-- do not write note content yet. "
            f"{self._language_instruction(context=context)} "
            f"{selection_paragraph}"
            "FOLLOW THE LECTURER'S ORDER. The student will sit next to this note with the "
            "lecturer's slides and recording, so the topics must come in the same sequence the "
            "lecturer taught them: list them by where each one is first properly taught in the "
            "session, from the start of the transcript to the end. The lecturer already chose a "
            "teaching sequence; keep it. Do not regroup the session by theme, do not move "
            "classifications, applications or summaries to the front or the back, and do not "
            "reshuffle topics to make the outline look more textbook-like. If the lecturer "
            "scattered one subject across several moments of the session, gather all of it into "
            "the topic at the position where it was FIRST taught. The only permitted departure "
            "from the lecturer's sequence is the rare case where a topic is unintelligible "
            "without a prerequisite the lecturer gave only later; then move that single "
            "prerequisite topic up, never more. "
            "EVERY TRANSCRIPT CHUNK BELONGS TO EXACTLY ONE TOPIC -- the topic that teaches its "
            "material. Never list the same chunk under two topics; when a later topic depends on "
            "an earlier one, say so in refers_back_to so it can refer back in a phrase instead "
            "of teaching the same thing twice. "
            "Remarks that are genuinely not course material (jokes, personal stories, class "
            "logistics, exam dates, attendance, greetings, technical problems with the "
            "recording) go under ONE single catch-all topic for them, placed last, instead of "
            "being dropped or disguised as course topics. If there are no such remarks, do not "
            "create that topic at all. "
            f"{topic_count_paragraph}"
            "A foundational idea belongs to exactly one topic -- the one that teaches it -- even "
            "if later topics rely on it. "
            f"{self._v4_planner_paragraph() if self._is_pedagogy_v4(context=context) else ''}"
            "Return JSON only."
        )

    def _generate_taught_unit_batch_for_topic(
        self,
        *,
        lecture: Lecture,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        topic: dict[str, Any],
        topic_batch: dict[str, Any],
        topic_index: int,
        total_topics: int,
        relevant_slide_pages: list[dict[str, Any]],
        topic_outline: list[dict[str, Any]],
        already_covered_digest: str,
        sub_batch_index: int = 1,
        total_sub_batches: int = 1,
        retry_attempt: int = 0,
        event_callback: Any | None = None,
        topic_allocation: dict[str, Any] | None = None,
        teaching_plan: dict[str, Any] | None = None,
        expand_hint: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], Any]:
        """Stage B of the professor-rewrite (v3) pipeline.

        Re-teaches the topic in its own order, wording and examples, aiming at
        the character budget the planner allocated. The budget is a target
        range: going far over made notes longer than their transcript, and
        going far under dropped taught material, so both are guarded.
        """
        is_split_topic = total_sub_batches > 1
        allocation = topic_allocation or {}
        importance = self._normalize_topic_importance(
            allocation.get("importance") or topic.get("importance")
        )
        # A split topic's budget is shared between its parts, not repeated.
        part_chars = max(
            220, int(allocation.get("target_chars", 1200)) // max(1, total_sub_batches)
        )
        part_words = max(40, part_chars // self._TEACHING_CHARS_PER_WORD)
        is_last_part = sub_batch_index >= total_sub_batches
        plan = teaching_plan or {}
        objectives = self._audit_list(plan.get("learning_objectives"))
        topic_objectives = [
            objectives[index]
            for index in (topic.get("objective_ids") or [])
            if isinstance(index, int) and 0 <= index < len(objectives)
        ]
        titles_by_topic_id = {
            str(other.get("topic_id", "")): str(other.get("title", "")) for other in topic_outline
        }
        refers_back_to = [
            titles_by_topic_id.get(str(value)) or str(value)
            for value in (topic.get("refers_back_to") or [])
            if str(value)
        ]

        summarized = self._note_detail_mode(context=context) == "summarized"
        if summarized:
            depth_options = {
                "core": (
                    "This topic is CORE: the lecture exists to teach it. Teach it fully -- the "
                    "motivating problem or intuition, the precise definition, how it works, one "
                    "complete worked example, and the mistake students typically make. "
                ),
                "supporting": (
                    "This topic is SUPPORTING: real course material, but not a learning "
                    "objective of its own. Give the definition and why it matters in a few "
                    "sentences. Do not include a worked example unless the point is impossible "
                    "to grasp without one. "
                ),
                "mention": (
                    "This topic is a MENTION: the lecturer named it in passing. Produce ONE "
                    "short unit that names the term and says in one or two sentences what it "
                    "is. No example, no note box, no recap. "
                ),
            }
        else:
            depth_options = {
                "core": (
                    "This topic is CORE: the lecture exists to teach it. Teach it completely -- "
                    "the motivating problem or intuition, the precise definition, how it works "
                    "step by step, every rule, formula, criterion and case the lecturer gave "
                    "for it, each worked example the lecturer used (set up fully and carried "
                    "to its result), and the mistakes and exam cautions that go with it. Give "
                    "each distinct concept its own unit; never fold two concepts into one. "
                ),
                "supporting": (
                    "This topic is SUPPORTING: course material the lecturer covered more "
                    "briefly. Still teach it properly: the definition or rule, why it matters, "
                    "how it is used, and the example the lecturer gave for it if there was one "
                    "(kept compact). Do not shrink it to a single sentence. "
                ),
                "mention": (
                    "This topic is a MENTION: the lecturer only named it in passing. Produce "
                    "one short unit that names the term and says what it is in a sentence or "
                    "two. If the excerpts actually define, explain or exemplify it, teach it "
                    "as supporting material instead. "
                ),
            }
        depth_contract = depth_options.get(importance) or (
            "This topic is OFF-CURRICULUM: it is the catch-all for remarks that are not "
            "course material. Produce only off-topic units, each a short faithful account "
            "of one remark, and teach nothing. "
        )

        split_topic_instruction = (
            (
                f"This topic's source material is long, so it is taught in {total_sub_batches} "
                f"consecutive parts and you are writing part {sub_batch_index} of "
                f"{total_sub_batches}. Teach only what the excerpts of THIS part contain, "
                "continuing naturally from what earlier parts already taught (see the digest "
                "below) without repeating it and without recapping the whole topic. "
            )
            if is_split_topic
            else ""
        )
        if self._is_pedagogy_v4(context=context):
            system_prompt = self._v4_writer_system_prompt(context=context, summarized=summarized)
            user_prompt = self._v4_writer_user_prompt(
                context=context,
                topic=topic,
                topic_batch=topic_batch,
                topic_index=topic_index,
                total_topics=total_topics,
                topic_outline=topic_outline,
                relevant_slide_pages=relevant_slide_pages,
                already_covered_digest=already_covered_digest,
                plan=plan,
                objectives=objectives,
                topic_objectives=topic_objectives,
                refers_back_to=refers_back_to,
                importance=importance,
                depth_contract=depth_contract,
                split_topic_instruction=split_topic_instruction,
                summarized=summarized,
                part_words=part_words,
                part_chars=part_chars,
                sub_batch_index=sub_batch_index,
                total_sub_batches=total_sub_batches,
                is_last_part=is_last_part,
                retry_attempt=retry_attempt,
                expand_hint=expand_hint,
            )
            return self._complete_teaching_json(
                provider=provider,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        system_prompt = (
            "You are an outstanding university professor writing the study note for a student "
            "who missed this lecture. The raw transcript excerpts below are everything the "
            "student can know about what the lecturer said: machine-generated speech-to-text, "
            "so it is in the lecturer's order, full of digressions, repetition, half-finished "
            "sentences, references to a board or slide the student cannot see, and ambiguous "
            "examples. "
            "Do NOT transcribe or paraphrase the transcript sentence by sentence. RE-TEACH the "
            "material the way a gifted professor of this very course would: your own clear "
            "explanations and transitions, but in the SAME SEQUENCE the lecturer followed, so the "
            "note can be read alongside the slides and the recording. Move a sentence out of "
            "the lecturer's order only to bring a scattered point next to its subject. "
            "TEACH LIKE A PROFESSOR WHO KNOWS THE BEST CURRENT PRACTICE FOR THIS SUBJECT: for "
            "each concept, in this order, say what it is and why it is needed (the problem it "
            "solves), define it precisely, build the intuition, show the formula or procedure "
            "with every symbol explained, then the worked example, then the typical mistake or "
            "exam trap. Use a unit's body for the idea and intuition, formulas for the "
            "equations, example for the worked case and note for the trap; skip a stage "
            "only when the lecturer gave nothing for it and nothing standard in this subject "
            "fills it. Define every symbol and every term the first time it appears, so a "
            "student who knows only the prerequisites can follow without the lecture. "
            f"{self._language_instruction(context=context)} "
            f"{self._teaching_detail_instruction(context=context)} "
            "(Any remark above about not adding external knowledge concerns translation "
            "fidelity only.) "
            f"{self._teaching_budget_paragraph(summarized=summarized, part_words=part_words, part_chars=part_chars)}"
            f"{depth_contract}"
            f"{self._teaching_survival_paragraph(summarized=summarized)}"
            f"{self._teaching_fidelity_paragraph()}"
            f"{self._beyond_lecturer_paragraph(context=context)}"
            "Every example you keep must be unambiguous: state the setup completely (what the "
            "data/objects/numbers are), walk through the steps, and give the result, so the "
            "student never needs the board. When the lecturer pointed at unseen board content, "
            "reconstruct it from the surrounding speech and the slides; if the exact values are "
            "not determined, use clearly labeled illustrative values instead of presenting "
            "guesses as the lecturer's data. "
            f"{self._teaching_example_policy(summarized=summarized)}"
            "OFF-CURRICULUM CONTENT: jokes, personal stories, class logistics (exam dates, "
            "attendance, grading, assignment deadlines), greetings, and chatter that are not "
            "course material must NEVER be mixed into teaching text. Put each such remark in "
            "its own unit with content_type \"off-topic\" (a short, faithful account in body; "
            "no note, example or list_items) -- only for a coherent remark of at least a full "
            "sentence, never for a single word such as \"music\", applause, unintelligible "
            "audio or a half-heard fragment, which you simply skip. If the excerpts contain "
            "none, produce none. "
            "The transcript can mishear technical terms as ordinary words (e.g. spoken "
            "notation or English terms rendered as a similar-sounding Persian word). When the "
            "context clearly implies the intended term, silently write the correct term; never "
            "quote a suspected mishearing. "
            f"{TERM_PRESERVATION_PROMPT}"
            "Writing style: clear, warm, precise and economical, like a textbook written by a "
            "gifted teacher who respects the reader's time. Write each unit's body as one or "
            "more short paragraphs (each paragraph one idea, at most about four sentences) "
            "separated by a blank line (the two characters \\n\\n inside the JSON string). Put "
            "enumerations and step sequences in list_items instead of burying them in prose. "
            "Use note for a caution or a key remark, example for a complete worked example. "
            "Put every key equation, law and calculation formula in the unit's formulas array "
            "(one string each), as the schema describes, so it is typeset as a display "
            "formula; whenever the lecturer works a numeric example, put the substituted "
            "formula with the real numbers there too. Inside running prose, mention a "
            "quantity by name or in plain ASCII (for instance z = (x - mean) / std), never "
            "with dollar signs. "
            "Never say the same thing twice: not in body and again in note or example, not "
            "across two units, and never a sentence that only announces what the next sentence "
            "says. "
            "Topic order is already planned; you are given the outline and a digest of what "
            "earlier topics taught. When this topic relies on something taught earlier, refer "
            "back to it in a short phrase instead of re-teaching it, and do not teach things "
            "that belong to a later topic of the outline. The excerpts you receive may contain "
            "sentences that belong to a different topic of the outline; teach those in their "
            "own topic, not here -- but anything that fits no topic at all must still be "
            "taught here rather than dropped. "
            f"{split_topic_instruction}"
            f"{self._teaching_selection_instruction(context=context)} "
            f"{self._removal_scope_instruction(context=context)} "
            "Return JSON only."
        )
        outline_preview = json.dumps(
            [
                {
                    "topic_id": other.get("topic_id", ""),
                    "title": other["title"],
                    "scope": other.get("scope", ""),
                    "importance": self._normalize_topic_importance(other.get("importance")),
                }
                for other in topic_outline
            ],
            ensure_ascii=False,
        )
        covered_digest = (
            already_covered_digest.strip() or "(nothing written yet -- this is the first topic)"
        )
        user_prompt = (
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"Course hints: {json.dumps(context['course_hints'], ensure_ascii=False)}\n"
            + (
                f"What this lecture is about: {plan['lecture_thesis']}\n"
                if plan.get("lecture_thesis")
                else ""
            )
            + (
                "Learning objectives for the whole note (the standard for what is worth "
                f"teaching): {json.dumps(objectives, ensure_ascii=False)}\n"
                if objectives
                else ""
            )
            + (
                "Objectives THIS topic must serve: "
                f"{json.dumps(topic_objectives, ensure_ascii=False)}\n"
                if topic_objectives
                else ""
            )
            + "Relevant slide pages for this topic (use them to recover terminology and to "
            "understand what the lecturer pointed at; they are not a script):\n"
            f"{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            f"Full topic outline for this note, in teaching order: {outline_preview}\n"
            + (
                "Already taught earlier -- refer back to these by name in a phrase, never "
                f"re-teach them: {json.dumps(refers_back_to, ensure_ascii=False)}\n"
                if refers_back_to
                else ""
            )
            + "Already taught in earlier topics/parts of this same note (concept titles only -- "
            f"refer back briefly, do not re-teach):\n{covered_digest}\n"
            f"You are teaching topic {topic_index} of {total_topics}: \"{topic['title']}\""
            + (
                f" (part {sub_batch_index} of {total_sub_batches})"
                if is_split_topic
                else ""
            )
            + f", importance tier: {importance}.\n"
            f"Intended scope: {topic['scope']}\n"
            f"Target for this pass: about {part_words} words / {part_chars} characters total "
            f"(aim for {int(part_words * 0.8)} to {int(part_words * 1.15)} words).\n"
            + self._teaching_expand_hint_text(expand_hint=expand_hint)
            + "Return JSON with keys, in this order: taught, omitted, document_title, units\n"
            "- taught: write this FIRST, as a private audit. An array of short strings, one for "
            "each thing from the excerpts you are teaching in units, each suffixed with its "
            "depth as \" [full]\", \" [brief]\" or \" [named]\"\n"
            + (
                "- omitted: an array of short strings, one for each thing in the excerpts you are "
                "deliberately NOT carrying into the note, each with a brief reason (e.g. "
                "\"lecturer restated the definition twice -- taught once\", \"third example "
                "of the same rule -- kept the clearest one\", \"aside about an unrelated tool "
                "-- serves no objective\")\n"
                "- units: an array of concept units in the order the lecturer taught them; "
                "use as FEW as teach this topic's objectives well, and no more\n"
                if summarized
                else "- omitted: an array of short strings for NOISE only -- repetition of a "
                "point already taught, filler, restarted sentences, greetings, logistics, "
                "non-course asides -- each with a brief reason. Never list a concept, "
                "definition, rule, formula, step, example, case, warning or exam hint here: "
                "those belong in units. An empty list is correct when the excerpts contain no "
                "noise\n"
                "- units: an array of concept units in the order the lecturer taught them; "
                "one unit per distinct concept, definition, rule, procedure, case or worked "
                "example, as many as the material needs, never folding two distinct concepts "
                "into one\n"
            )
            +             "- each unit is an object with keys: concept_title, section_title, content_type, "
            "body, formulas, code_blocks, tables, list_items, note, example, example_ai, "
            "ai_added, slide_pages\n"
            "- content_type must be one of: definition, explanation, comparison, process, "
            "list, warning, example, figure, table, formula, summary, off-topic\n"
            "- concept_title: a clear heading for what this unit teaches\n"
            "- body: the teaching text; several short paragraphs separated by a blank line "
            "(\\n\\n) are encouraged. A single plain string, never an object or array\n"
            f"{FORMULA_FIELD_PROMPT}"
            "- list_items: plain strings only, never objects\n"
            "- note and example: each a single plain string; a worked example is flowing "
            "prose that states the setup, walks through the steps, and gives the result\n"
            "- example_ai: true only when the example is one you reconstructed or solved "
            "yourself because the lecturer did not give it; false otherwise\n"
            "- ai_added: an array of plain strings, each one short addition of your own "
            "(see the six ways above); [] when you added nothing, which is the normal case\n"
            f"- {self._language_instruction(context=context)}\n"
            f"- {self._teaching_detail_instruction(context=context)}\n"
            "- NEVER output raw transcript formatting, timestamps or speaker labels\n"
            "- Use content_type off-topic only for the off-curriculum remarks described in "
            "the system prompt, and never write a [[slide:N]] marker in them\n"
            "- Use slide_pages only from the slide pages provided above, and only when a "
            "slide clearly matches the unit; otherwise leave it empty\n"
            + (
                "- Close with a brief recap of this topic's key takeaways, as the last unit "
                "or a unit of content_type summary, only if the topic is substantial and only "
                "if it says something the units did not already say\n"
                if is_last_part and importance in {"core", "supporting"}
                else "- Do NOT write a recap or summary unit in this pass\n"
            )
            + self._spoken_english_terms_hint(context=context, text=str(topic_batch["text"]))
            + self._lecturer_cues_hint(context=context, text=str(topic_batch["text"]), topic_batch=topic_batch)
            + (
                f"Transcript excerpts for THIS PART ({sub_batch_index} of "
                f"{total_sub_batches}) of the topic:\n{topic_batch['text']}"
                if is_split_topic
                else f"Transcript excerpts for this topic:\n{topic_batch['text']}"
            )
        )
        user_prompt = self._retry_prompt_prefix(retry_attempt) + user_prompt
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        else:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        return payload, llm_result

    @staticmethod
    def _complete_teaching_json(
        *,
        provider: GapGPTNoteProvider,
        system_prompt: str,
        user_prompt: str,
        event_callback: Any | None,
    ) -> tuple[dict[str, Any], Any]:
        if "event_callback" in inspect.signature(provider.complete_json).parameters:
            return provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        return provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)

    # -- v4: pedagogy-first teaching style -----------------------------------

    def _is_pedagogy_v4(self, *, context: dict[str, Any]) -> bool:
        return str(context.get("note_pedagogy", "") or "") == "v4"

    def _v4_planner_paragraph(self) -> str:
        return (
            "SCOPE IS A LEARNING GOAL: write each topic's scope as what a student should be "
            "able to explain or do after studying it (for example \"tell X from Y and apply "
            "rule Z to a new case\"), in one short phrase -- the writer will choose the "
            "clearest way to teach it, so do not prescribe sentences or examples. "
        )

    @staticmethod
    def _v4_board_example_rule() -> str:
        return (
            "BOARD EXAMPLES WHOSE INPUT WAS NEVER SPOKEN: often the lecturer works an example "
            "on the board and the transcript keeps only the results or fragments of it (the "
            "counts, the winner, the output, \"this pair\", \"so we get this\") while the "
            "problem itself -- the input data, the table, the sequence, the numbers -- was "
            "only written, never said. A note that states only the result cannot be learned "
            "from, so never write such an example as a bare outcome. Instead rebuild a "
            "complete example yourself, working backwards from what the lecturer did say: "
            "(1) collect every number, name, choice and result the excerpts give for the "
            "example; (2) invent the smallest concrete input that reproduces ALL of them when "
            "the lecturer's method is applied to it -- every count, total, winner and final "
            "result must come out exactly as stated, so recompute your input step by step "
            "before you write it and change it until nothing disagrees; (3) put it in the "
            "unit's example field: first the problem with its concrete input, then the "
            "solution step by step using the lecturer's own method and terms, ending in the "
            "result the lecturer gave; (4) begin it with a short label in the note language "
            "saying that the input is a reconstruction consistent with the lecturer's results "
            "(in Persian: «نمونهٔ بازسازی‌شده»), so it is never presented as the lecturer's "
            "own data. The same applies the other way round: when only the problem was "
            "spoken and the answer was not, solve it and give the answer. Use "
            "this only for an example the lecturer actually worked; never invent an example "
            "for a point they did not illustrate, keep it as short as the method allows, and "
            "if the stated results are too few or too inconsistent to fit any input, show "
            "the method on a tiny labeled illustrative input and state only the results the "
            "lecturer gave. Do not mention this process to the reader. "
        )

    def _v4_writer_system_prompt(self, *, context: dict[str, Any], summarized: bool) -> str:
        prompt = self._v4_writer_system_prompt_base(context=context, summarized=summarized)
        prompt = apply_v4_shape_rules(apply_story_rule(prompt, context), context)
        return add_quality_rules_to_writer(prompt, context)

    def _v4_writer_system_prompt_base(self, *, context: dict[str, Any], summarized: bool) -> str:
        """Static-per-lecture system prompt for the v4 topic writer.

        Everything topic-specific (budget, depth tier, part number) lives in the
        user prompt's tail so this text and the user prompt's head stay
        byte-identical across all topics of a lecture and can be prefix-cached.
        """
        return (
            "You are an outstanding university professor and a master of instructional "
            "design, writing the study note for a student who missed this lecture and has "
            "very little time. The student has only your note. The raw transcript excerpts "
            "you receive are machine-generated speech-to-text: in the lecturer's spoken "
            "order, with digressions, repetition, half-finished sentences and references to "
            "a board or slide the student cannot see. "
            "YOUR JOB IS TO TEACH, NOT TO TRANSCRIBE. The order of the MAIN topics is "
            "already fixed to follow the lecture, but INSIDE a topic you are free to teach "
            "in whatever order and wording makes the idea easiest to learn -- you are not "
            "bound to the order the lecturer happened to speak in, and you must never "
            "paraphrase the transcript sentence by sentence. Everything the lecturer taught "
            "must still be in the note; only the way it is taught is yours. "
            "THE TEACHING PLAYBOOK (what the best textbooks of every field do), applied "
            "to each concept in roughly this arc, skipping a step only when it adds nothing: "
            "(1) WHY FIRST -- open with the problem or question the concept answers, in one "
            "or two sentences, so the student knows why they are about to learn it. "
            "(2) PLAIN BEFORE FORMAL -- say it in everyday words first (a short everyday "
            "analogy only when it genuinely clarifies, and never one that could mislead), "
            "then give the precise definition with the proper term, introducing each term "
            "right where it is first needed and defining every symbol and abbreviation at "
            "first use. "
            "(3) CONCRETE BEFORE ABSTRACT -- show the smallest concrete case or number before "
            "stating the general rule when the lecturer gave one. "
            "(4) HOW IT WORKS -- a procedure becomes an ORDERED list with one action per "
            "step (list_ordered true); a set of parallel items, types or conditions becomes "
            "an unordered list; two or more things compared on the same attributes become a "
            "table; every key equation goes in formulas with its symbols explained once in "
            "the body. "
            "(5) WORKED EXAMPLE -- state the setup completely, walk the steps, give the "
            "result, and end with one sentence on what the example shows. "
            "(6) CONFUSABLES AND TRAPS -- when two ideas are easily mixed up, say in one or "
            "two sentences how they differ; put the typical mistake or exam trap in note. "
            "(7) KEY IDEA -- put the single sentence a student must remember into key_idea, "
            "in plain words (see the schema). "
            "THE LEARNING TEST: before you finish a unit, picture a student who missed the "
            "lecture and reads only this unit. They must be able to (a) explain the idea in "
            "their own words, (b) say why it is true or why it is needed, and (c) apply it to "
            "a similar question. If any of the three would fail, close the gap -- usually "
            "with one missing step, the 'why' behind a rule, a definition of a term the "
            "lecturer assumed, or the common mistake -- and put what you supply that the "
            "lecturer did not say into ai_added. Never close a gap with vague praise or "
            "generic background. "
            "WRITE LEAN. A long note is not a good note: the student cannot afford many "
            "pages. Every sentence must carry new information. Do not announce what you are "
            "about to say, do not recap what you just said, do not pad with generic "
            "background, do not repeat a point across body, note, example, list_items and "
            "key_idea, and do not add analogies or examples the lecturer did not need. Short "
            "sentences, one idea per paragraph, at most three sentences per paragraph. The "
            "budget in the user message is a size to land in; reach it with teaching, not with "
            "words. "
            f"{self._language_instruction(context=context)} "
            f"{self._teaching_detail_instruction(context=context)} "
            "(Any remark above about not adding external knowledge concerns translation "
            "fidelity only.) "
            f"{self._teaching_survival_paragraph(summarized=summarized)}"
            f"{self._teaching_fidelity_paragraph()}"
            f"{self._beyond_lecturer_paragraph(context=context)}"
            "Every example you keep must be unambiguous: state the setup completely, walk "
            "through the steps and give the result, so the student never needs the board. "
            f"{self._v4_board_example_rule()}"
            f"{self._teaching_example_policy(summarized=summarized)}"
            "OFF-CURRICULUM CONTENT: jokes, personal stories, class logistics (exam dates, "
            "attendance, grading, deadlines), greetings and chatter that are not course "
            "material must NEVER be mixed into teaching text. Put each such remark in its own "
            "unit with content_type \"off-topic\" (a short, faithful account in body; no note, "
            "example, list_items or key_idea) -- only for a coherent remark of at least a "
            "full sentence, never for a single word, applause, unintelligible audio or a "
            "half-heard fragment, which you simply skip. If there is none, produce none. "
            "The transcript can mishear technical terms as ordinary words. When the context "
            "clearly implies the intended term, silently write the correct term; never quote "
            "a suspected mishearing. "
            f"{TERM_PRESERVATION_PROMPT}"
            "Use the two characters \\n\\n inside a JSON string for a paragraph break. Put "
            "every key equation, law and calculation formula in the unit's formulas array "
            "(one string each), and whenever the lecturer works a numeric example, put the "
            "substituted formula with the real numbers there too. Inside running prose, "
            "mention a quantity by name or in plain ASCII (for instance z = (x - mean) / "
            "std), never with dollar signs. "
            "Topic order is already planned. You get the outline and a digest of what "
            "earlier topics taught: when this topic relies on something taught earlier, "
            "refer back in a short phrase instead of re-teaching it, and do not teach "
            "things that belong to a later topic. Excerpts may contain sentences that "
            "belong to another topic of the outline; teach those in their own topic, not "
            "here -- but anything that fits no topic at all must still be taught here "
            "rather than dropped. "
            f"{self._teaching_selection_instruction(context=context)} "
            f"{self._removal_scope_instruction(context=context)} "
            "OUTPUT: return JSON only, an object with keys, in this order: taught, "
            "document_title, units.\n"
            "- taught: write this FIRST, as a private completeness checklist: an array of "
            "very short phrases (a few words each), one per distinct thing from the "
            "excerpts that you are teaching in units, each suffixed with its depth as "
            "\" [full]\", \" [brief]\" or \" [named]\"\n"
            "- document_title: a short title for the lecture\n"
            "- units: an array of concept units, one per distinct concept, definition, rule, "
            "procedure, case or worked example, never folding two distinct concepts into one"
            + (" and using as few as teach the topic's objectives well" if summarized else "")
            + "\n"
            "- each unit is an object with keys: concept_title, section_title, content_type, "
            "key_idea, body, formulas, code_blocks, tables, list_items, list_ordered, note, "
            "example, example_ai, ai_added, slide_pages\n"
            "- content_type is one of: definition, explanation, comparison, process, list, "
            "warning, example, figure, table, formula, summary, off-topic\n"
            "- concept_title: a clear heading for what this unit teaches\n"
            "- key_idea: ONE plain sentence (at most about 25 words) giving the core idea of "
            "the topic in everyday words. Write it ONLY in the single unit that carries the "
            "topic's central idea (usually its first), and \"\" in every other unit; never "
            "restate the definition word for word, and \"\" for off-topic units\n"
            "- body: the teaching text, a few short paragraphs separated by a blank line "
            "(\\n\\n). A single plain string, never an object or array\n"
            f"{FORMULA_FIELD_PROMPT}"
            "- list_items: plain strings only, never objects\n"
            "- list_ordered: true only when list_items are sequential steps of a procedure; "
            "false for parallel items (types, conditions, properties)\n"
            "- note and example: each a single plain string; a worked example is flowing "
            "prose that states the setup, walks through the steps and gives the result\n"
            "- example_ai: true only when the example is one you reconstructed or solved "
            "yourself because the lecturer did not give it; false otherwise\n"
            "- ai_added: an array of plain strings, each one short addition of your own "
            "(see the six ways above). These TesaNote additions are how the note fills what "
            "the lecturer left implicit, so use them wherever THE LEARNING TEST shows a gap -- "
            "most core concepts gain from one well-chosen addition (the intuition, the "
            "missing step or the typical mistake) -- but never as filler and never to repeat "
            "body; [] when the lecturer's own teaching already passes the test\n"
            "- NEVER output raw transcript formatting, timestamps or speaker labels\n"
            "- Use content_type off-topic only for the off-curriculum remarks described "
            "above, and never write a [[slide:N]] marker in them\n"
            "- Use slide_pages only from the slide pages provided in the user message, and "
            "only when a slide clearly matches the unit; otherwise leave it empty\n"
        )

    def _v4_writer_user_prompt(
        self,
        *,
        context: dict[str, Any],
        topic: dict[str, Any],
        topic_batch: dict[str, Any],
        topic_index: int,
        total_topics: int,
        topic_outline: list[dict[str, Any]],
        relevant_slide_pages: list[dict[str, Any]],
        already_covered_digest: str,
        plan: dict[str, Any],
        objectives: list[str],
        topic_objectives: list[str],
        refers_back_to: list[str],
        importance: str,
        depth_contract: str,
        split_topic_instruction: str,
        summarized: bool,
        part_words: int,
        part_chars: int,
        sub_batch_index: int,
        total_sub_batches: int,
        is_last_part: bool,
        retry_attempt: int,
        expand_hint: dict[str, Any] | None,
    ) -> str:
        is_split_topic = total_sub_batches > 1
        outline_preview = json.dumps(
            [
                {
                    "topic_id": other.get("topic_id", ""),
                    "title": other["title"],
                    "scope": other.get("scope", ""),
                    "importance": self._normalize_topic_importance(other.get("importance")),
                }
                for other in topic_outline
            ],
            ensure_ascii=False,
        )
        covered_digest = (
            already_covered_digest.strip() or "(nothing written yet -- this is the first topic)"
        )
        # Lecture-wide part first (identical for every topic -> cacheable prefix).
        head = (
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"Course hints: {json.dumps(context['course_hints'], ensure_ascii=False)}\n"
            + (
                f"What this lecture is about: {plan['lecture_thesis']}\n"
                if plan.get("lecture_thesis")
                else ""
            )
            + (
                "Learning objectives for the whole note (the standard for what is worth "
                f"teaching): {json.dumps(objectives, ensure_ascii=False)}\n"
                if objectives
                else ""
            )
            + f"Full topic outline for this note, in teaching order: {outline_preview}\n"
        )
        tail = (
            self._teaching_budget_paragraph(
                summarized=summarized, part_words=part_words, part_chars=part_chars
            )
            + depth_contract
            + split_topic_instruction
            + "\n"
            + (
                "Objectives THIS topic must serve: "
                f"{json.dumps(topic_objectives, ensure_ascii=False)}\n"
                if topic_objectives
                else ""
            )
            + "Relevant slide pages for this topic (use them to recover terminology and to "
            "understand what the lecturer pointed at; they are not a script):\n"
            f"{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            + (
                "Already taught earlier -- refer back to these by name in a phrase, never "
                f"re-teach them: {json.dumps(refers_back_to, ensure_ascii=False)}\n"
                if refers_back_to
                else ""
            )
            + "Already taught in earlier topics/parts of this same note (concept titles only -- "
            f"refer back briefly, do not re-teach):\n{covered_digest}\n"
            f"You are teaching topic {topic_index} of {total_topics}: \"{topic['title']}\""
            + (f" (part {sub_batch_index} of {total_sub_batches})" if is_split_topic else "")
            + f", importance tier: {importance}.\n"
            f"Intended scope: {topic['scope']}\n"
            f"Target for this pass: about {part_words} words / {part_chars} characters total "
            f"(aim for {int(part_words * 0.8)} to {int(part_words * 1.15)} words).\n"
            + self._teaching_expand_hint_text(expand_hint=expand_hint)
            + (
                "Close with a brief recap of this topic's key takeaways, as the last unit "
                "or a unit of content_type summary, only if the topic is substantial and only "
                "if it says something the units did not already say.\n"
                if is_last_part and importance in {"core", "supporting"}
                else "Do NOT write a recap or summary unit in this pass.\n"
            )
            + self._retry_prompt_prefix(retry_attempt)
            + self._spoken_english_terms_hint(context=context, text=str(topic_batch["text"]))
            + self._lecturer_cues_hint(context=context, text=str(topic_batch["text"]), topic_batch=topic_batch)
            + (
                f"Transcript excerpts for THIS PART ({sub_batch_index} of "
                f"{total_sub_batches}) of the topic:\n{topic_batch['text']}"
                if is_split_topic
                else f"Transcript excerpts for this topic:\n{topic_batch['text']}"
            )
        )
        return head + tail

    def _generate_content_unit_batch(
        self,
        *,
        lecture: Lecture,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        batch: dict[str, Any],
        batch_index: int,
        total_batches: int,
        relevant_slide_pages: list[dict[str, Any]],
        retry_attempt: int = 0,
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], Any]:
        system_prompt = (
            "You are a source-grounded lecture-note editor. "
            "You are processing only one continuous portion of a lecture. "
            "Convert only this transcript portion and the provided slide evidence into polished notes under the selected detail policy. "
            f"{self._language_instruction(context=context)} "
            f"{self._detail_mode_instruction(context=context)} "
            "Do NOT add external textbook definitions, examples, explanations, or prior knowledge. "
            "Do NOT define a term unless the professor explicitly defined or explained it in this transcript batch, or the provided slide pages directly state that definition. "
            "If a term is only mentioned, mention it briefly without inventing a definition. "
            f"{self._source_selection_instruction(context=context)} "
            "Transcript is the primary source of truth. Use slides only to disambiguate unclear transcript wording, recover technical terms, complete incomplete phrases, and attach correct slide grounding. "
            "The transcript is machine-generated speech-to-text and can mishear a technical term "
            "as an unrelated ordinary word, especially short notation spoken aloud (e.g. "
            "complexity notation like O(n), O(n^2), O(log n), Omega, Theta can be misheard as an "
            "ordinary Persian word that does not fit the sentence). "
            "When a transcript word does not make sense in its technical context and the "
            "surrounding transcript or slide evidence clearly implies the intended term, silently "
            "write the correct term instead of the misheard word. "
            "Never wrap a suspected mis-transcription in quotation marks as if it were the "
            "professor's intended term; either correct it or, if you cannot confidently recover "
            "it, drop it rather than quoting nonsense. "
            f"{SPEECH_TO_TEXT_CORRECTION_PROMPT}"
            "For a worked example, read the entire local transcript portion as one sequence and combine its setup, board references, substitutions, intermediate steps, and conclusion. "
            "Reconstruct omitted board writing only when the result is logically determined by transcript or slide evidence; otherwise write a clearly labeled generalized reconstruction and do not invent exact values. "
            "Never let slides replace the transcript. Never generate a concept only because it exists on slides unless the transcript for this batch clearly mentions it or strongly implies it. "
            "All output must be written in the selected output language. "
            f"{TERM_PRESERVATION_PROMPT}"
            "Within this batch, each concept should appear once, completely, not repeated in multiple partial fragments. "
            f"{self._removal_scope_instruction(context=context)} "
            "Return JSON only."
        )
        user_prompt = (
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"Course hints: {json.dumps(context['course_hints'], ensure_ascii=False)}\n"
            f"Relevant slide pages for this batch:\n{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            f"{self._lecture_terminology_hint(context=context)}"
            f"{self._spoken_english_terms_hint(context=context, text=str(batch.get('text', '') or ''))}"
            f"You are processing part {batch_index} of {total_batches}.\n"
            'Return JSON with keys: document_title, units\n'
            "- units must be an array of concept units\n"
            "- each unit must be an object with keys: concept_title, section_title, content_type, body, formulas, code_blocks, tables, list_items, note, example, slide_pages\n"
            "- content_type must be one of: definition, explanation, comparison, process, list, warning, example, figure, table, formula, summary, off-topic\n"
            f"{FORMULA_FIELD_PROMPT}"
            "- list_items must be plain strings only, never objects\n"
            "- body, note, and example must each be a single plain prose string, never a JSON "
            "object or array. Write a worked example as flowing sentences (state the problem, "
            "walk through the steps, give the result in prose), not as a structured object with "
            "sub-fields such as problem/setup/steps/result\n"
            f"- {self._language_instruction(context=context)}\n"
            f"- {self._detail_mode_instruction(context=context)}\n"
            "- Every text field must use the selected output language. Do not dump untranslated slide bullets unless the output language requires them\n"
            f"- {TERM_PRESERVATION_PROMPT}\n"
            "- NEVER output raw transcript formatting or speaker labels\n"
            f"{self._batch_mode_rules(context=context, batch_text=str(batch.get('text', '') or ''))}"
            "- Build the content from the transcript batch first. Use slides only to fix unclear words, restore terminology, and attach references\n"
            "- Do not import whole slide sections that are not clearly supported by this transcript batch\n"
            "- Do not add definitions, explanations, examples, caveats, or applications that are not present in the transcript batch or relevant slide pages\n"
            "- If a term is mentioned without explanation, keep it brief and do not expand it into a textbook explanation\n"
            "- If this is part 1, include the lecture introduction as one concept unit\n"
            "- If this is not part 1, continue naturally without repeating the full introduction\n"
            "- Use content_type off-topic only for a coherent casual, personal, or "
            "administrative discussion unrelated to course learning; write a concise "
            "faithful account in body, leave note/example/list_items empty, and do not "
            "attach slide_pages. Off-topic content is never tied to a slide, so never write "
            "a [[slide:N]] marker anywhere in its body text either\n"
            "- Off-topic content is usually near the start or end of class. Do not classify teaching, required course logistics, assessment requirements, or safety instructions as off-topic\n"
            "- If a concept has an example or caution, attach it in example or note instead of repeating the concept in another unit\n"
            "- An example field must be self-contained: include the problem/setup plus the supported steps and result, not a dangling phrase copied from speech\n"
            "- If the professor points to unseen board content, use nearby speech and slide OCR/formulas together. Never fabricate missing numbers; use variables and label a generalized reconstruction when exact values are absent\n"
            "- If the source contains enumerations or categories, put them into list_items in the same unit instead of scattering them across prose\n"
            "- Use slide_pages only from the relevant slide pages provided above\n"
            "- Attach slide pages only when the related slide is genuinely clear from the transcript and support context. If unsure, leave slide_pages empty\n"
            "- Prefer transcript wording when it is clear. Use slide wording only to repair unclear or incomplete transcript text\n"
            f"Transcript part text:\n{batch['text']}"
        )
        user_prompt = self._retry_prompt_prefix(retry_attempt) + user_prompt
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        else:
            payload, llm_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        if self._batch_payload_is_too_sparse(
            payload=payload,
            batch_text=str(batch.get("text", "") or ""),
            context=context,
        ):
            payload, llm_result = self._regenerate_sparse_content_unit_batch(
                lecture=lecture,
                context=context,
                provider=provider,
                batch=batch,
                batch_index=batch_index,
                total_batches=total_batches,
                relevant_slide_pages=relevant_slide_pages,
                event_callback=event_callback,
            )
        return payload, llm_result

    @staticmethod
    def _slide_context_snippet(*, context: dict[str, Any] | None = None, pages: list[dict[str, Any]] | None = None) -> str:
        pages = pages if pages is not None else (context or {}).get("slide_context", {}).get("pages", [])
        if not pages:
            return "No slides were uploaded."
        lines: list[str] = []
        for page in pages[:12]:
            lines.append(
                f"Page {page['page_number']}: {str(page.get('text', '') or '')[:600]}\n"
                f"Headings: {json.dumps(page.get('heading_lines', [])[:6], ensure_ascii=False)}\n"
                f"Bullets: {json.dumps(page.get('bullet_lines', [])[:8], ensure_ascii=False)}\n"
                f"Keywords: {json.dumps(page.get('keywords', [])[:10], ensure_ascii=False)}\n"
                f"Formulas: {json.dumps(page.get('formulas', [])[:5], ensure_ascii=False)}\n"
                f"Tables: {json.dumps(page.get('table_text', [])[:2], ensure_ascii=False)}"
            )
        return "\n\n".join(lines)

    def _minimum_unit_count_for_batch(self, batch_text: str, context: dict[str, Any] | None = None) -> int:
        length = len(str(batch_text or ""))
        if context is not None and self._note_detail_mode(context=context) == "summarized":
            if length < 2200:
                return 1
            if length < 5200:
                return 2
            return 3
        if length < 1400:
            return 1
        if length < 2800:
            return 2
        if length < 4200:
            return 3
        if length < 5600:
            return 4
        return 5

    def _batch_payload_is_too_sparse(
        self,
        *,
        payload: dict[str, Any],
        batch_text: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        units = self._extract_content_units_from_batch_payload(payload)
        if not units:
            return True
        # A concise response is the expected result for summarized notes. The
        # detailed-mode sparsity retry used to expand it back into a full note.
        if context is not None and self._note_detail_mode(context=context) == "summarized":
            return False
        minimum_units = self._minimum_unit_count_for_batch(batch_text, context=context)
        detail_chars = 0
        for unit in units:
            detail_chars += len(str(unit.get("body", "") or ""))
            detail_chars += len(str(unit.get("note", "") or ""))
            detail_chars += len(str(unit.get("example", "") or ""))
            detail_chars += sum(len(str(item or "")) for item in (unit.get("list_items") or []))
        compression_ratio = detail_chars / max(1, len(batch_text))
        return len(units) < minimum_units and compression_ratio < 0.22

    def _build_note_generation_input_audit(
        self,
        *,
        context: dict[str, Any],
        batches: list[dict[str, Any]],
    ) -> dict[str, Any]:
        transcript_chunks = context.get("transcript_chunks", []) or []
        batch_summaries: list[dict[str, Any]] = []
        overlap_ratios: list[dict[str, Any]] = []
        duplicated_ranges: list[dict[str, Any]] = list(
            (context.get("transcript_overlap_audit", {}) or {}).get(
                "duplicated_character_ranges", []
            )
            or []
        )

        previous_batch: dict[str, Any] | None = None
        for index, batch in enumerate(batches, start=1):
            text = str(batch.get("text", "") or "")
            batch_summaries.append(
                {
                    "batch_index": index,
                    "chunk_indexes": list(batch.get("chunk_indexes", []) or []),
                    "start_ts": float(batch.get("start_ts", 0.0) or 0.0),
                    "end_ts": float(batch.get("end_ts", 0.0) or 0.0),
                    "text_hash": self._text_hash(text),
                    "text_char_count": len(text),
                }
            )
            if previous_batch is not None:
                ratio = self._text_overlap_ratio(
                    left=str(previous_batch.get("text", "") or ""),
                    right=text,
                )
                time_overlap = max(
                    0.0,
                    float(previous_batch.get("end_ts", 0.0) or 0.0)
                    - float(batch.get("start_ts", 0.0) or 0.0),
                )
                overlap_ratios.append(
                    {
                        "previous_batch_index": index - 1,
                        "batch_index": index,
                        "text_overlap_ratio": round(ratio, 4),
                        "time_overlap_seconds": round(time_overlap, 3),
                    }
                )
                if ratio >= 0.12:
                    duplicated_ranges.append(
                        {
                            "previous_batch_index": index - 1,
                            "batch_index": index,
                            "duplicate_kind": "semantic_batch_text_overlap",
                            "text_overlap_ratio": round(ratio, 4),
                        }
                    )
                if time_overlap > 0:
                    duplicated_ranges.append(
                        {
                            "previous_batch_index": index - 1,
                            "batch_index": index,
                            "duplicate_kind": "semantic_batch_time_overlap",
                            "time_overlap_seconds": round(time_overlap, 3),
                        }
                    )
            previous_batch = batch

        return {
            "transcript_chunk_count": len(transcript_chunks),
            "semantic_batch_count": len(batches),
            "batches": batch_summaries,
            "batch_overlap_ratios": overlap_ratios,
            "duplicated_character_ranges": duplicated_ranges[:120],
            "repeated_transcript_sentences_count": self._repeated_sentence_count(
                str(context.get("transcript_text", "") or "")
            ),
            "transcript_overlap_audit": context.get("transcript_overlap_audit", {}) or {},
        }

    def _validate_batch_unit_grounding(
        self,
        *,
        units: list[dict[str, Any]],
        batch: dict[str, Any],
        relevant_slide_pages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        batch_text = str(batch.get("text", "") or "")
        slide_text = self._slide_plain_text(pages=relevant_slide_pages)
        source_hash = self._text_hash(batch_text)
        retained_units: list[dict[str, Any]] = []
        removed_count = 0
        model_added_count = 0
        grounded_transcript_count = 0
        grounded_slide_count = 0
        unit_reports: list[dict[str, Any]] = []

        for unit in units:
            unit_text = self._raw_unit_text(unit)
            transcript_overlap = self._transcript_overlap_ratio(
                text=unit_text,
                transcript_text=batch_text,
            )
            slide_overlap = self._transcript_overlap_ratio(
                text=unit_text,
                transcript_text=slide_text,
            )
            grounded_in_transcript = transcript_overlap >= 0.12
            grounded_in_slides = slide_overlap >= 0.12
            model_added = (
                not grounded_in_transcript
                and not grounded_in_slides
                and self._looks_like_generic_textbook_unit(unit_text)
            )
            if model_added:
                model_added_count += 1
                if not self._unit_has_source_backed_value(unit=unit, slide_overlap=slide_overlap):
                    removed_count += 1
                    unit_reports.append(
                        {
                            "concept_title": str(unit.get("concept_title", "") or unit.get("title", "") or ""),
                            "source_text_hash": source_hash,
                            "grounded_in_transcript": grounded_in_transcript,
                            "grounded_in_slides": grounded_in_slides,
                            "model_added": True,
                            "removed": True,
                            "transcript_overlap_ratio": round(transcript_overlap, 4),
                            "slide_overlap_ratio": round(slide_overlap, 4),
                        }
                    )
                    continue
            if grounded_in_transcript:
                grounded_transcript_count += 1
            if grounded_in_slides:
                grounded_slide_count += 1
            current = deepcopy(unit)
            current["source_text_hash"] = source_hash
            current["grounding"] = {
                "grounded_in_transcript": grounded_in_transcript,
                "grounded_in_slides": grounded_in_slides,
                "model_added": model_added,
                "transcript_overlap_ratio": round(transcript_overlap, 4),
                "slide_overlap_ratio": round(slide_overlap, 4),
            }
            retained_units.append(current)
            unit_reports.append(
                {
                    "concept_title": str(unit.get("concept_title", "") or unit.get("title", "") or ""),
                    "source_text_hash": source_hash,
                    **current["grounding"],
                    "removed": False,
                }
            )

        return retained_units, {
            "batch_index": batch.get("batch_index", 0),
            "input_unit_count": len(units),
            "output_unit_count": len(retained_units),
            "removed_model_added_unit_count": removed_count,
            "model_added_unit_count": model_added_count,
            "grounded_in_transcript_unit_count": grounded_transcript_count,
            "grounded_in_slide_unit_count": grounded_slide_count,
            "source_text_hash": source_hash,
            "units": unit_reports[:80],
        }

    def _summarize_unit_grounding_reports(self, reports: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "batch_reports": reports,
            "input_unit_count": sum(int(report.get("input_unit_count", 0) or 0) for report in reports),
            "output_unit_count": sum(int(report.get("output_unit_count", 0) or 0) for report in reports),
            "model_added_unit_count": sum(int(report.get("model_added_unit_count", 0) or 0) for report in reports),
            "removed_model_added_unit_count": sum(
                int(report.get("removed_model_added_unit_count", 0) or 0) for report in reports
            ),
            "grounded_in_transcript_unit_count": sum(
                int(report.get("grounded_in_transcript_unit_count", 0) or 0) for report in reports
            ),
            "grounded_in_slide_unit_count": sum(
                int(report.get("grounded_in_slide_unit_count", 0) or 0) for report in reports
            ),
        }

    def _text_hash(self, text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()[:16]

    def _unit_debug_snapshot(self, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for index, unit in enumerate(units[:160]):
            snapshots.append(
                {
                    "index": index,
                    "concept_title": str(unit.get("concept_title", "") or ""),
                    "section_title": str(unit.get("section_title", "") or ""),
                    "content_type": str(unit.get("content_type", "") or ""),
                    "body_hash": self._text_hash(str(unit.get("body", "") or "")),
                    "source_text_hash": str(unit.get("source_text_hash", "") or ""),
                    "slide_pages": list(unit.get("slide_pages", []) or []),
                    "chunk_indexes": list(unit.get("chunk_indexes", []) or []),
                    "start_ts": float(unit.get("start_ts", 0.0) or 0.0),
                    "end_ts": float(unit.get("end_ts", 0.0) or 0.0),
                    "grounding": dict(unit.get("grounding", {}) or {}),
                }
            )
        return snapshots

    def _block_debug_snapshot(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for index, block in enumerate(blocks[:220]):
            block_text = " ".join(
                [
                    str(block.get("title", "") or ""),
                    str(block.get("content", "") or ""),
                    str(block.get("caption", "") or ""),
                    " ".join(
                        str(item.get("title", "") if isinstance(item, dict) else item)
                        for item in block.get("items", [])
                        if str(item).strip()
                    )
                    if isinstance(block.get("items"), list)
                    else "",
                ]
            )
            snapshots.append(
                {
                    "index": index,
                    "type": str(block.get("type", "") or ""),
                    "title": str(block.get("title", "") or ""),
                    "text_hash": self._text_hash(block_text),
                }
            )
        return snapshots

    def _text_overlap_ratio(self, *, left: str, right: str) -> float:
        left_tokens = self._keyword_set(left)
        right_tokens = self._keyword_set(right)
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))

    def _repeated_sentence_count(self, text: str) -> int:
        normalized_counts: dict[str, int] = {}
        for sentence in self._split_into_sentences(text):
            key = self._concept_key(sentence)
            if key:
                normalized_counts[key] = normalized_counts.get(key, 0) + 1
        return sum(count - 1 for count in normalized_counts.values() if count > 1)

    def _slide_plain_text(self, *, pages: list[dict[str, Any]]) -> str:
        parts: list[str] = []
        for page in pages:
            parts.extend(
                [
                    str(page.get("text", "") or ""),
                    " ".join(page.get("heading_lines", []) or []),
                    " ".join(page.get("bullet_lines", []) or []),
                    " ".join(page.get("keywords", []) or []),
                    " ".join(page.get("candidate_concepts", []) or []),
                    " ".join(page.get("formula_text", []) or []),
                    " ".join(page.get("table_text", []) or []),
                ]
            )
        return " ".join(part for part in parts if part).strip()

    def _raw_unit_text(self, unit: dict[str, Any]) -> str:
        return " ".join(
            part
            for part in [
                str(unit.get("concept_title", "") or ""),
                str(unit.get("section_title", "") or ""),
                str(unit.get("key_idea", "") or ""),
                str(unit.get("body", "") or unit.get("content", "") or ""),
                str(unit.get("note", "") or ""),
                str(unit.get("example", "") or ""),
                " ".join(str(item) for item in unit.get("list_items", []) if str(item).strip()),
            ]
            if part
        ).strip()

    def _looks_like_generic_textbook_unit(self, text: str) -> bool:
        lowered = str(text or "").lower()
        if self._is_definition_like_sentence(lowered):
            return True
        return bool(
            re.search(
                r"(به طور کلی|در واقع|معمولاً|معمولا|یکی از مفاهیم|از دیدگاه علمی|"
                r"به عنوان یک مفهوم|is a concept|generally|typically|in software engineering)",
                lowered,
                re.IGNORECASE,
            )
        )

    def _unit_has_source_backed_value(self, *, unit: dict[str, Any], slide_overlap: float) -> bool:
        if slide_overlap >= 0.2:
            return True
        if str(unit.get("example", "") or "").strip() and not self._looks_like_generic_textbook_unit(
            str(unit.get("example", "") or "")
        ):
            return True
        if any(str(item or "").strip() for item in unit.get("list_items", []) or []):
            return True
        if unit.get("tables"):
            return True
        if unit.get("code_blocks"):
            # Code the lecturer dictated never overlaps the Persian transcript word for word.
            return True
        return False

    @staticmethod
    def _transcript_chunk_snippet(*, context: dict[str, Any]) -> str:
        chunks = context["transcript_chunks"]
        if not chunks:
            return "No transcript chunks are available."
        lines: list[str] = []
        for chunk in chunks[:16]:
            text = str(chunk["text"]).strip().replace("\n", " ")
            if len(text) > 550:
                text = f"{text[:550].rsplit(' ', 1)[0]}…"
            lines.append(
                f"Chunk {chunk['chunk_index']} ({chunk['start_ts']:.2f}s - {chunk['end_ts']:.2f}s): {text}"
            )
        return "\n".join(lines)

    @staticmethod
    def _validate_semantic_document(payload: dict[str, Any]) -> None:
        pages = payload.get("pages")
        if not isinstance(pages, list) or not pages:
            raise PipelineError(stage="NOTE_GENERATION", message="Semantic note output is missing pages.")
        for page in pages:
            if not isinstance(page, dict):
                raise PipelineError(stage="NOTE_GENERATION", message="Each semantic page must be an object.")
            if not isinstance(page.get("blocks"), list):
                raise PipelineError(stage="NOTE_GENERATION", message="Each semantic page must contain a blocks array.")

    def _should_use_chunked_semantic_generation(self, *, context: dict[str, Any]) -> bool:
        transcript_text = str(context.get("transcript_text", "") or "")
        transcript_chunks = context.get("transcript_chunks") or []
        duration_seconds = 0.0
        if transcript_chunks:
            duration_seconds = float(transcript_chunks[-1].get("end_ts", 0.0) or 0.0)
        return (
            len(transcript_text) >= min(settings.NOTE_LONG_TRANSCRIPT_THRESHOLD_CHARS, 6000)
            or len(transcript_chunks) > 4
            or duration_seconds >= 12 * 60
        )

    def _semantic_batch_char_limit(self, *, context: dict[str, Any]) -> int:
        default_limit = int(settings.NOTE_SEMANTIC_BATCH_CHARS)
        if self._note_detail_mode(context=context) == "summarized":
            # Larger coherent batches let the model merge related minor points
            # instead of producing one detailed unit for every short chunk.
            if context.get("has_slides"):
                return max(6000, min(max(default_limit, 9000), 9000))
            return max(7000, min(max(default_limit, 10000), 10000))
        if context.get("has_slides"):
            return max(4200, min(default_limit, 6000))
        return max(3600, min(default_limit, 4800))

    def _input_language_code(self, *, context: dict[str, Any]) -> str:
        return str(context.get("lecture", {}).get("input_language") or "fa")

    def _output_language_code(self, *, context: dict[str, Any]) -> str:
        return str(context.get("lecture", {}).get("output_language") or "fa")

    def _note_detail_mode(self, *, context: dict[str, Any]) -> str:
        mode = str(context.get("lecture", {}).get("note_detail_mode") or "detailed")
        return "summarized" if mode == "summarized" else "detailed"

    def _output_direction(self, *, context: dict[str, Any]) -> str:
        return "rtl" if is_rtl_language(self._output_language_code(context=context)) else "ltr"

    def _language_instruction(self, *, context: dict[str, Any]) -> str:
        input_code = self._input_language_code(context=context)
        output_code = self._output_language_code(context=context)
        input_label = language_label(input_code, locale="en")
        output_label = language_label(output_code, locale="en")
        return (
            f"The transcript language is {input_label} ({input_code}). "
            f"The final note language must be {output_label} ({output_code}). "
            "If these languages differ, faithfully translate the professor's meaning into the final note language. "
            "Do not add external textbook knowledge while translating."
        )

    def _detail_mode_instruction(self, *, context: dict[str, Any]) -> str:
        if self._note_detail_mode(context=context) == "summarized":
            return (
                "Generate a genuinely condensed structured study note, not a rewritten transcript and not the detailed note with shorter sentences. "
                "Keep only major taught topics, essential definitions, key comparisons, exam-worthy points, central formulas/tables, and the minimum examples required for understanding. "
                "Merge related minor points into their parent topic and remove repeated reasoning, secondary examples, tangents, filler, and repeated wording. "
                "Aim for roughly 25 to 35 percent of the meaningful detail of a normal detailed note while preserving the lecture's main learning outcomes."
            )
        return "Generate the normal detailed note and preserve the current detailed behavior."

    def _lecture_terminology_hint(self, *, context: dict[str, Any]) -> str:
        """Lecture-wide English terms, so a batch can spell a garbled term the way the rest of the lecture does."""
        if self._output_language_code(context=context) == "en":
            return ""
        terms = spoken_english_terms(str(context.get("transcript_text", "") or ""), limit=80)
        arabic = self._lecture_arabic_terms_hint(context=context)
        if not terms:
            return arabic
        return (
            "Terms written in Latin letters anywhere in the whole lecture transcript (use them "
            "to spell and recognise the same terms correctly in this part): "
            f"{json.dumps(terms, ensure_ascii=False)}\n"
        ) + arabic

    def _lecture_arabic_terms_hint(self, *, context: dict[str, Any]) -> str:
        """Revision 3: the Arabic words and phrases of the whole lecture, so a garbled one is spelled as elsewhere."""
        if not quality_rev_v42(context):
            return ""
        terms = arabic_terms(str(context.get("transcript_text", "") or ""), limit=80)
        if not terms:
            return ""
        return (
            "Arabic words and phrases the lecturer said anywhere in the lecture (keep each in Arabic "
            "script, spelled as here, and use them to recognise the same words in this part): "
            f"{json.dumps(terms, ensure_ascii=False)}\n"
        )

    def _spoken_english_terms_hint(self, *, context: dict[str, Any], text: str) -> str:
        """List the English terms the lecturer said in these excerpts, so none is translated away."""
        if self._output_language_code(context=context) == "en":
            return ""
        terms = spoken_english_terms(text)
        if quality_rev_v42(context):
            # Revision 3 names every foreign word (Arabic as well as Latin) and states the rule for all.
            return foreign_terms_hint(text)
        if not terms:
            return ""
        return (
            "English terms the lecturer said in these excerpts (the foreign-term rule applies "
            "to each one; write it in Latin letters, beside its Persian wording when you "
            f"translate it): {json.dumps(terms, ensure_ascii=False)}. Words the transcript "
            "spelled phonetically in Persian script count too.\n"
        )

    def _lecturer_cues_hint(
        self, *, context: dict[str, Any], text: str, topic_batch: dict[str, Any] | None = None
    ) -> str:
        """Revision 3: the quiz, exam, homework, deadline and stressed-point remarks in these excerpts.

        The QA reviews found these missing or misfiled in most notes; the same finder is used to
        measure them, so what the writer is asked to keep is what is checked.
        """
        if not quality_rev_v42(context):
            return ""
        # The same hook carries the damaged-audio notice: both are facts about these excerpts.
        marks = ""
        if topic_batch is not None and getattr(settings, "NOTE_QUALITY_V42_CHUNK_MARKS_ENABLED", False):
            # Phase B1: name the broken stretches inside the excerpts (the excerpt-level notice only
            # fires when most of the whole excerpt is broken).
            marks = damaged_chunks_prompt(self._batch_chunk_texts(context=context, topic_batch=topic_batch))
        return required_remarks_prompt(text) + damaged_excerpt_prompt(text) + marks

    def _teaching_detail_instruction(self, *, context: dict[str, Any]) -> str:
        """Detail policy for v3.

        Detailed means complete teaching of everything the lecturer taught,
        sized by the planned budget. An earlier version leaned on "be selective"
        and "go to the shortest form", and the model read that as licence to
        drop whole sub-sections; the length ceiling is enforced in code, so the
        prompt does not need to push the model downward as well.
        """
        if self._note_detail_mode(context=context) == "summarized":
            return (
                "Write a genuinely condensed note: only the major taught topics, the essential "
                "definitions and formulas, and the single example a topic cannot be understood "
                "without."
            )
        return (
            "Write a thorough, complete study note: teach every concept the lecturer taught "
            "properly, with its definitions, how it works step by step, its rules and formulas, "
            "and the worked examples the lecturer used. Remove only repetition, filler and "
            "non-course chatter -- never a concept, rule, example or exam hint."
        )

    def _teaching_selection_instruction(self, *, context: dict[str, Any]) -> str:
        """Source-selection policy for v3."""
        if self._note_detail_mode(context=context) == "summarized":
            return (
                "Keep only what is necessary to review this topic's learning outcomes. Do not "
                "retain a detail merely because it is present in the source."
            )
        return (
            "Keep everything the lecturer taught that a student needs to learn and be examined "
            "on: definitions, mechanisms, procedures, comparisons, formulas, worked examples, "
            "rules and exam-relevant cautions. What you drop is noise -- repetition, filler, "
            "restarted sentences and non-course remarks -- not course content."
        )

    def _teaching_budget_paragraph(
        self, *, summarized: bool, part_words: int, part_chars: int
    ) -> str:
        if summarized:
            return (
                "SELECTION, NOT REPRODUCTION. This is a condensed note. "
                f"YOUR BUDGET FOR THIS PASS IS ABOUT {part_words} WORDS ({part_chars} "
                "characters) across all units, and it binds. When the material does not fit, "
                "do NOT truncate an explanation and do NOT drop a taught concept silently: "
                "merge related minor points into one general statement, and state a secondary "
                "point in a single sentence where you would otherwise write a paragraph. "
            )
        return (
            "COMPLETENESS FIRST, THEN TIGHTNESS. The student has only your note, so a concept "
            "the lecturer taught that is missing from it cannot be learned. Repetition, "
            "thinking aloud, restarted sentences and the same point made three ways are noise "
            "and you remove them; everything the lecturer actually taught stays. "
            f"YOUR TARGET FOR THIS PASS IS ABOUT {part_words} WORDS ({part_chars} characters) "
            f"across all units -- land between {int(part_words * 0.8)} and "
            f"{int(part_words * 1.15)} words. Being far UNDER the target means you left taught "
            "material out: before you answer, re-read the excerpts for any definition, rule, "
            "step, example or caution you have not yet written down, and teach it. Being over "
            "means tighten wording and merge repeats; it never means dropping a concept. "
        )

    def _teaching_survival_paragraph(self, *, summarized: bool) -> str:
        if summarized:
            return (
                "WHAT MUST SURVIVE AT ANY LENGTH: the concepts, definitions, formulas, rules, "
                "numbers and warnings that a student needs to meet this topic's learning "
                "objectives, and every example that is the only demonstration of its "
                "technique. Everything else is yours to compress or omit. "
            )
        return (
            "WHAT MUST SURVIVE: every concept, definition, rule, formula, criterion, step, "
            "number, case, named example, comparison, warning, tip and exam hint that the "
            "lecturer gave in these excerpts. They may be re-worded and re-ordered, but not "
            "removed or reduced to a bare name. "
        )

    def _beyond_lecturer_paragraph(self, *, context: dict[str, Any]) -> str:
        """What the writer may add beyond the lecturer's words (v3 and v4).

        Every addition goes into ai_added (and example_ai) so the reader can see
        it did not come from the lecturer. This applies to every field of study;
        students who want only the lecturer's words use v2.
        """
        return TESANOTE_ADDITIONS_PROMPT

    def _teaching_fidelity_paragraph(self) -> str:
        return SIMPLE_TONE_PROMPT + ONE_HOME_PROMPT + (
            "FIGURES AND LISTS: keep every number, quantity, interval, percentage, size, "
            "dose, count, threshold, date and every named list or classification the "
            "lecturer gave, together with what it measures or counts. Never replace one "
            "with a vague phrase such as \"it varies\" or \"should not be assumed the "
            "same for everyone\". If speech-to-text garbled a figure, use the reading the "
            "context supports; if it truly cannot be determined, state the point without the "
            "figure instead of inventing one. "
            "TEXTBOOK VOICE: state the material as established subject matter. Do not narrate "
            "the class (\"in the lecture it was mentioned\", \"the lecturer said\") and do "
            "not hedge a point merely because the transcript was noisy or the evidence was "
            "spoken; the one exception is a point the lecturer explicitly stressed as "
            "important or examinable, which you may flag briefly in a note. "
            "UNCLEAR OR BROKEN PASSAGES: the student studies for an exam from this note alone, "
            "so a taught point must never be lost because the transcript is garbled, cut off "
            "or incomplete. Restore what the lecturer evidently meant from the surrounding "
            "speech, the slides and the rest of the lecture, and teach it completely. "
            "ORIENTATION IS COURSE MATERIAL: what the subject is, how it is classified and "
            "named, synonyms and definitions of terms, and any one-sentence factual remark "
            "about the subject (for example that X is associated with Y) are taught in a unit; "
            "none of them is off-topic. "
        )

    def _teaching_example_policy(self, *, summarized: bool) -> str:
        if summarized:
            return (
                "An example that would cost more words than it teaches should be replaced by "
                "one sentence stating what it demonstrates. "
            )
        return (
            "Keep every distinct example the lecturer worked through; drop one only when it "
            "merely repeats an example you already kept for the same point. "
        )

    @staticmethod
    def _teaching_expand_hint_text(*, expand_hint: dict[str, Any] | None) -> str:
        if not expand_hint:
            return ""
        titles = [str(title) for title in (expand_hint.get("previous_titles") or []) if title]
        missing = [str(item) for item in (expand_hint.get("missing_figures") or []) if item]
        reason = (
            f"was only about {int(expand_hint.get('previous_words', 0))} words against a "
            f"target of about {int(expand_hint.get('target_words', 0))}"
            if expand_hint.get("target_words")
            else "left out many of the specific figures the lecturer gave"
        )
        return (
            "IMPORTANT -- THIS IS A SECOND ATTEMPT. Your previous answer for these exact "
            f"excerpts {reason}, which means taught "
            "material was left out or taught too thinly. Write the COMPLETE note for these "
            "excerpts now: every definition, rule, step, worked example, case and caution the "
            "lecturer gave, each taught properly. "
            + (
                "Your previous attempt covered only these concepts, so expect to cover more: "
                f"{json.dumps(titles, ensure_ascii=False)}. "
                if titles
                else ""
            )
            + (
                "The excerpts contain these figures that your previous attempt dropped or "
                "replaced with vaguer wording; every one that is part of the subject matter "
                "must now appear, with what it measures: "
                f"{json.dumps(missing, ensure_ascii=False)}. "
                if missing
                else ""
            )
            + "\n"
        )

    def _source_selection_instruction(self, *, context: dict[str, Any]) -> str:
        if self._note_detail_mode(context=context) == "summarized":
            return (
                "Select only source-backed information that is necessary to understand or review the lecture's major learning outcomes. "
                "Do not retain a detail merely because it is present in the source."
            )
        return (
            "Preserve source-backed explanations, definitions, examples, mechanisms, comparisons, formulas, hardware details, and exam-relevant cautions."
        )

    def _removal_scope_instruction(self, *, context: dict[str, Any]) -> str:
        if self._note_detail_mode(context=context) == "summarized":
            return (
                "Remove minor elaborations, repeated reasoning, secondary examples, greetings, filler, and repeated logistics. "
                "Keep a coherent non-course side discussion only in an unrelated-discussion-box, never inside the academic note or final summary."
            )
        return (
            "Delete greetings, farewells, filler, and repeated logistics. Keep a coherent non-course side discussion only in an "
            "unrelated-discussion-box, never inside the academic note or final summary."
        )

    def _direct_mode_rules(self, *, context: dict[str, Any]) -> str:
        if self._note_detail_mode(context=context) == "summarized":
            return (
                "- organize the summary around major topics rather than following every transcript turn\n"
                "- combine related explanations into one concise block and omit minor repetitions and secondary examples\n"
                "- keep no more than one essential worked example per major topic, and omit examples that do not add a distinct learning outcome\n"
                "- prefer compact paragraphs and short lists; every block must contribute a distinct high-value point\n"
            )
        return (
            "- preserve the teaching flow, not just bullet summaries\n"
            "- preserve source-backed academic detail, including distinctions, constraints, definitions, examples, mappings, terminology, and exam-relevant caveats\n"
            "- DO NOT compress a long lecture into a short summary. Preserve the actual taught content across multiple sections\n"
        )

    def _direct_structure_rules(self, *, context: dict[str, Any]) -> str:
        common_example_rule = (
            "- every retained worked example must contain a usable scenario and its supported reasoning or result\n"
            "- if an essential example depends on unseen whiteboard writing, use exact slide/OCR/formula evidence when available; otherwise use a generalized reconstruction without fabricated numbers\n"
        )
        if self._note_detail_mode(context=context) == "summarized":
            return (
                "- first page should include a chapter header and a concise lead paragraph; supporting boxes are optional\n"
                "- use an example-box only for an essential example that materially improves understanding of a major topic\n"
                "- use note-box or exam-point only for a distinct high-value caution or exam point\n"
                f"{common_example_rule}"
            )
        return (
            "- remove greetings, farewells, filler, and repeated logistics; use unrelated-discussion-box for a coherent non-course discussion the student may choose to skip\n"
            "- first page should normally include a chapter-header, a lead introductory paragraph, and at least one supporting rich block such as note-box or key-highlight\n"
            "- when the source contains a scenario, conditional explanation, or worked example, use an example-box instead of burying it inside a long paragraph\n"
            f"{common_example_rule}"
            "- when the source contains a caution, implementation detail, important reminder, or exam-relevant point, use note-box or exam-point\n"
            "- a lecture-length note should usually use at least 4 distinct block types whenever the source supports them\n"
        )

    def _batch_mode_rules(self, *, context: dict[str, Any], batch_text: str) -> str:
        minimum_units = self._minimum_unit_count_for_batch(batch_text, context=context)
        if self._note_detail_mode(context=context) == "summarized":
            return (
                f"- produce about {minimum_units} concise major-topic unit(s) for this batch; merge subordinate points into those units\n"
                "- target about 20 to 30 percent of the meaningful source length, not a near-complete rewrite\n"
                "- keep each body focused on the definition, mechanism, comparison, formula, or conclusion needed for review\n"
                "- include an example only if it is essential to understanding a major topic; omit secondary practice and repeated demonstrations\n"
            )
        return (
            "- keep the note faithful to this part of the lecture and preserve technical content thoroughly\n"
            f"- produce enough distinct concept units to cover the source-backed content in this batch; usually at least {minimum_units} unless the batch contains fewer genuinely explained concepts\n"
            "- do not collapse this batch into a short summary; cover each distinct taught concept once and completely\n"
            "- body text inside each unit may be several sentences long when needed; preserve substance, not just headings\n"
            "- preserve nearly all meaningful source-backed academic information from this part; remove filler and repeated logistics, and emit a separate off-topic unit for a coherent non-course discussion\n"
        )

    def _retrieve_relevant_slide_pages(
        self,
        *,
        context: dict[str, Any],
        text: str,
        top_k: int,
    ) -> list[dict[str, Any]]:
        slide_pages = context.get("slide_context", {}).get("pages", [])
        if not isinstance(slide_pages, list) or not slide_pages:
            return []
        text_tokens = self._keyword_set(text)
        scored_pages: list[tuple[float, dict[str, Any]]] = []
        for page in slide_pages:
            if not isinstance(page, dict):
                continue
            page_text = " ".join(
                [
                    str(page.get("text", "") or ""),
                    " ".join(page.get("heading_lines", []) or []),
                    " ".join(page.get("bullet_lines", []) or []),
                    " ".join(page.get("keywords", []) or []),
                    " ".join(page.get("candidate_concepts", []) or []),
                    " ".join(page.get("formula_text", []) or []),
                    " ".join(page.get("table_text", []) or []),
                ]
            )
            page_tokens = self._keyword_set(page_text)
            overlap = len(text_tokens & page_tokens)
            score = overlap
            if page.get("heading_lines"):
                score += 1.4
            if page.get("bullet_lines"):
                score += 0.6
            if page.get("table_text"):
                score += 0.4
            if page.get("formula_text"):
                score += 0.2
            if score >= 2:
                scored_pages.append((score, page))
        scored_pages.sort(key=lambda item: (item[0], item[1].get("page_number", 0)), reverse=True)
        return [page for _score, page in scored_pages[:top_k]]

    _UNIT_LIST_KEYS = (
        "units", "concept_units", "content_units", "concepts", "items", "sections", "blocks",
    )

    def _extract_content_units_from_batch_payload(self, payload: Any) -> list[dict[str, Any]]:
        # Models occasionally return the units under a different key, as a
        # bare array, or as one unit object -- all recoverable without
        # another model call, so don't treat them as "no units".
        if isinstance(payload, list):
            candidates: Any = payload
        elif isinstance(payload, dict):
            candidates = None
            for key in self._UNIT_LIST_KEYS:
                if isinstance(payload.get(key), list):
                    candidates = payload[key]
                    break
            if candidates is None:
                if payload.get("concept_title") and (payload.get("body") or payload.get("content")):
                    candidates = [payload]
                else:
                    return []
        else:
            return []
        return [unit for unit in candidates if isinstance(unit, dict)]

    _MAX_UNIT_RECOVERY_RETRIES = 2
    _PROVIDER_FAILED_KEY = "_provider_failed"
    _MIN_FALLBACK_SOURCE_CHARS = 200

    _FIGURE_PATTERN = re.compile(r"\d+(?:[.,٫]\d+)?")
    _FIGURE_LABEL_BEFORE = re.compile(
        r"(?:slide|page|p\.|اسلاید|صفحه|شکل|جدول|figure|fig\.|table)\s*$", re.IGNORECASE
    )
    _MIN_FIGURE_GAP_COUNT = 3
    _MIN_FIGURE_GAP_SHARE = 0.35

    @classmethod
    def _normalize_digits(cls, text: str) -> str:
        return (
            str(text or "")
            .translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
            .replace("٫", ".")
        )

    @classmethod
    def _source_figures(cls, text: str) -> dict[str, str]:
        """Substantive numbers in the source mapped to a short context snippet.

        Single digits and slide/page/figure/table numbers are skipped: they are
        numbering, not subject-matter figures worth recalling.
        """
        normalized = cls._normalize_digits(text)
        figures: dict[str, str] = {}
        for match in cls._FIGURE_PATTERN.finditer(normalized):
            token = match.group(0).replace(",", ".")
            if len(token.replace(".", "")) < 2 and "." not in token:
                continue
            if cls._FIGURE_LABEL_BEFORE.search(normalized[max(0, match.start() - 14) : match.start()]):
                continue
            if token not in figures:
                start = max(0, match.start() - 40)
                figures[token] = normalized[start : match.end() + 25].replace("\n", " ").strip()
        return figures

    @classmethod
    def _missing_figures(cls, *, source_text: str, written_text: str) -> dict[str, str]:
        figures = cls._source_figures(source_text)
        if not figures:
            return {}
        written = {
            token.replace(",", ".")
            for token in cls._FIGURE_PATTERN.findall(cls._normalize_digits(written_text))
        }
        return {token: context for token, context in figures.items() if token not in written}

    def _batch_chunk_texts(self, *, context: dict[str, Any], topic_batch: dict[str, Any]) -> list[str]:
        by_index = {int(chunk.get("chunk_index", 0)): chunk for chunk in context.get("transcript_chunks", []) or []}
        return [
            str(by_index[index].get("text", "") or "")
            for index in topic_batch.get("chunk_indexes", []) or []
            if index in by_index and str(by_index[index].get("text", "") or "").strip()
        ]

    def _expand_short_v2_part(
        self,
        *,
        first_result: tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any], dict[str, Any]],
        generate_part: Any,
        topic: dict[str, Any],
        topic_batch: dict[str, Any],
        relevant_slide_pages: list[dict[str, Any]],
        label: str,
        chunk_texts: list[str] | None = None,
    ) -> tuple[
        tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any], dict[str, Any]],
        dict[str, Any] | None,
    ]:
        """v2.1: write a window once more when it came back far shorter than what was spoken,
        or when one of its spoken passages is missing from what was written.

        v2 is the lecturer's own sentences; a window at a fraction of the spoken length is a
        paraphrase or a summary. The retry is optional: any failure keeps the first answer, and
        the longer of the two answers wins.
        """
        units, reports, results, payload = first_result
        if any(unit.get("recovered_from_transcript") for unit in units):
            return first_result, None
        taught = [
            unit for unit in units if str(unit.get("content_type", "") or "").strip().lower() != "off-topic"
        ]
        written_words = sum(len(self._raw_unit_text(unit).split()) for unit in taught)
        written_text = " ".join(self._raw_unit_text(unit) for unit in taught)
        source_text = str(topic_batch.get("text", "") or "")
        is_short = v2_part_is_short(source_text=source_text, written_words=written_words)
        missing = passages_missing_from_written(chunk_texts or [], written_text)
        if not is_short and not missing:
            return first_result, None
        source_words = len(source_text.split())
        record: dict[str, Any] = {
            "topic_id": topic.get("topic_id"),
            "kind": "v2_length" if is_short else "v2_missing_passage",
            "source_words": source_words,
            "words_before": written_words,
            "missing_passages": len(missing),
            "retried": True,
            "used_retry": False,
        }
        hint: dict[str, Any] = {"missing_passages": missing}
        if is_short:
            hint.update({"source_words": source_words, "written_words": written_words})
        try:
            retry_units, retry_reports, retry_results, retry_payload = self._units_with_recovery(
                generate=partial(generate_part, expand_hint=hint),
                batch=topic_batch,
                relevant_slide_pages=relevant_slide_pages,
                label=f"{label} (length)",
                fallback_title=str(topic.get("title", "")),
                enforce_grounding=True,
            )
        except Exception:
            logger.warning("note.v2_length.retry_failed label=%s", label, exc_info=True)
            record["error"] = True
            return first_result, record
        retry_taught = [
            unit for unit in retry_units if str(unit.get("content_type", "") or "").strip().lower() != "off-topic"
        ]
        retry_words = sum(len(self._raw_unit_text(unit).split()) for unit in retry_taught)
        record["words_after"] = retry_words
        all_results = results + retry_results
        retry_missing = passages_missing_from_written(
            chunk_texts or [], " ".join(self._raw_unit_text(unit) for unit in retry_taught)
        )
        record["missing_after"] = len(retry_missing)
        # Longer wins for a short part; for a part with a missing passage the retry must bring
        # it back without losing much of the rest.
        better = (
            retry_words > written_words * V2_RETRY_GAIN
            if is_short
            else len(retry_missing) < len(missing) and retry_words >= written_words * 0.9
        )
        if better and not any(unit.get("recovered_from_transcript") for unit in retry_units):
            record["used_retry"] = True
            return (retry_units, retry_reports, all_results, retry_payload), record
        return (units, reports, all_results, payload), record

    def _expand_thin_teaching_part(
        self,
        *,
        first_result: tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any], dict[str, Any]],
        generate_part: Any,
        topic: dict[str, Any],
        topic_batch: dict[str, Any],
        relevant_slide_pages: list[dict[str, Any]],
        part_index: int,
        part_target_chars: int,
        label: str,
    ) -> tuple[
        tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any], dict[str, Any]],
        dict[str, Any] | None,
    ]:
        """Write a v3 topic part once more when it came back far below its target.

        The condense pass only ever shrinks, so a model that under-delivers --
        compressing a whole sub-section into a sentence or dropping its example --
        was never corrected, and a 40-page note became a 7-page one. The target
        is capped by what the excerpts can justify, so a short source is never
        asked to pad. The retry is optional: any failure keeps the first answer.
        """
        units, reports, results, payload = first_result
        importance = self._normalize_topic_importance(topic.get("importance"))
        if not settings.NOTE_V3_EXPAND_THIN_TOPICS or importance not in {"core", "supporting"}:
            return first_result, None
        if any(unit.get("recovered_from_transcript") for unit in units):
            return first_result, None
        taught_units = [
            unit
            for unit in units
            if str(unit.get("content_type", "") or "").strip().lower() != "off-topic"
        ]
        written_chars = sum(len(self._raw_unit_text(unit)) for unit in taught_units)
        source_chars = len(str(topic_batch.get("text", "") or ""))
        expected_chars = min(
            part_target_chars, int(source_chars * self._TEACHING_MAX_TOPIC_SOURCE_SHARE)
        )
        min_fill = float(settings.NOTE_V3_MIN_FILL_RATIO)
        is_thin = expected_chars >= 600 and written_chars < expected_chars * min_fill
        source_text = str(topic_batch.get("text", "") or "")
        written_text = " ".join(self._raw_unit_text(unit) for unit in taught_units)
        source_figure_count = len(self._source_figures(source_text))
        missing_figures = (
            self._missing_figures(source_text=source_text, written_text=written_text)
            if source_chars >= 600 and source_figure_count
            else {}
        )
        has_figure_gap = (
            len(missing_figures) >= self._MIN_FIGURE_GAP_COUNT
            and len(missing_figures) / source_figure_count >= self._MIN_FIGURE_GAP_SHARE
        )
        if not is_thin and not has_figure_gap:
            return first_result, None
        record: dict[str, Any] = {
            "topic_id": topic.get("topic_id"),
            "part": part_index,
            "importance": importance,
            "expected_chars": expected_chars,
            "chars_before": written_chars,
            "retried": True,
            "used_retry": False,
            "reason": "thin" if is_thin else "figure_gap",
            "missing_figures_before": len(missing_figures),
        }
        hint = {
            "previous_words": written_chars // self._TEACHING_CHARS_PER_WORD,
            "target_words": (expected_chars // self._TEACHING_CHARS_PER_WORD) if is_thin else 0,
            "previous_titles": [
                str(unit.get("concept_title", "") or "").strip() for unit in taught_units
            ][:30],
            "missing_figures": [
                f"{token} (\"...{context}...\")" for token, context in list(missing_figures.items())[:25]
            ],
        }
        try:
            retry_units, retry_reports, retry_results, retry_payload = self._units_with_recovery(
                generate=partial(generate_part, expand_hint=hint),
                batch=topic_batch,
                relevant_slide_pages=relevant_slide_pages,
                label=f"{label} (expansion)",
                fallback_title=str(topic.get("title", "")),
                enforce_grounding=False,
            )
        except Exception:
            logger.warning("note.teaching_budget.expand_failed label=%s", label, exc_info=True)
            record["error"] = True
            return first_result, record
        retry_taught = [
            unit
            for unit in retry_units
            if str(unit.get("content_type", "") or "").strip().lower() != "off-topic"
        ]
        retry_chars = sum(len(self._raw_unit_text(unit)) for unit in retry_taught)
        record["chars_after"] = retry_chars
        # Both attempts' provider calls count towards usage either way.
        all_results = results + retry_results
        retry_missing = self._missing_figures(
            source_text=source_text,
            written_text=" ".join(self._raw_unit_text(unit) for unit in retry_taught),
        )
        record["missing_figures_after"] = len(retry_missing)
        recovered_figures = (
            has_figure_gap
            and len(retry_missing) < len(missing_figures)
            and retry_chars >= written_chars * 0.9
        )
        if (retry_chars > written_chars * 1.15 or recovered_figures) and not any(
            unit.get("recovered_from_transcript") for unit in retry_units
        ):
            record["used_retry"] = True
            return (
                retry_units,
                reports + retry_reports,
                all_results,
                retry_payload if isinstance(retry_payload, dict) else {},
            ), record
        return (units, reports + retry_reports, all_results, payload), record

    def _retry_prompt_prefix(self, retry_attempt: int) -> str:
        if retry_attempt <= 0:
            return ""
        return (
            "IMPORTANT: your previous answer for these exact excerpts contained no usable "
            "concept units. The excerpts below are real lecture content. Return a JSON "
            "object with a non-empty \"units\" array: write at least one concept unit "
            "faithfully summarizing what the professor said here. If the excerpts are "
            "only casual or administrative talk, return one unit with content_type "
            "\"off-topic\" instead of an empty array.\n"
        )

    def _units_with_recovery(
        self,
        *,
        generate: Any,
        batch: dict[str, Any],
        relevant_slide_pages: list[dict[str, Any]],
        label: str,
        fallback_title: str,
        enforce_grounding: bool = True,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Any], dict[str, Any]]:
        """Run one batch generation so a bad model answer never aborts the whole job.

        An empty or unusable answer (empty array, unexpected shape, or every
        unit removed by the grounding filter) is retried with a firmer
        prompt. If it is still empty, a substantive source is preserved as a
        faithful transcript-based unit rather than silently dropped, and a
        trivial one (a few words of filler) is skipped; neither fails the job.
        """
        reports: list[dict[str, Any]] = []
        results: list[Any] = []
        payload: dict[str, Any] = {}
        provider_failure = ""
        for attempt in range(self._MAX_UNIT_RECOVERY_RETRIES + 1):
            try:
                payload, llm_result = generate(retry_attempt=attempt)
            except PipelineError as exc:
                if exc.stage != "NOTE_GENERATION":
                    raise
                # The provider router already retried and fell back; asking
                # again would only burn the lecture's time budget.
                provider_failure = exc.message or "provider failed"
                logger.warning(
                    "note_generation_provider_failed label=%s error=%s", label, provider_failure
                )
                break
            results.append(llm_result)
            units = self._extract_content_units_from_batch_payload(payload)
            if enforce_grounding:
                units, report = self._validate_batch_unit_grounding(
                    units=units,
                    batch=batch,
                    relevant_slide_pages=relevant_slide_pages,
                )
            else:
                # Teaching mode adds its own explanations and examples, so a
                # "must overlap the transcript" filter would delete exactly
                # the content the mode exists to produce.
                report = {
                    "input_unit_count": len(units),
                    "output_unit_count": len(units),
                    "grounding_skipped": True,
                }
            reports.append(report)
            if units:
                return units, reports, results, payload if isinstance(payload, dict) else {}
            logger.warning(
                "note_generation_empty_units label=%s attempt=%s", label, attempt + 1
            )
        recovered_payload = dict(payload) if isinstance(payload, dict) else {}
        if provider_failure:
            recovered_payload[self._PROVIDER_FAILED_KEY] = provider_failure
        source_text = self._clean_generated_text(str(batch.get("text", "") or "").strip())
        if len(source_text) < self._MIN_FALLBACK_SOURCE_CHARS:
            return [], reports, results, recovered_payload
        logger.warning("note_generation_transcript_fallback label=%s", label)
        fallback_unit = {
            "concept_title": fallback_title,
            "content_type": "explanation",
            "body": source_text,
            "list_items": [],
            "note": "",
            "example": "",
            "slide_pages": [],
            "recovered_from_transcript": True,
        }
        return [fallback_unit], reports, results, recovered_payload

    def _normalize_content_unit(
        self,
        *,
        unit: dict[str, Any],
        batch: dict[str, Any],
        batch_index: int,
        order_in_batch: int,
        relevant_slide_pages: list[dict[str, Any]],
        teaching_mode: bool = False,
    ) -> dict[str, Any]:
        concept_title = str(unit.get("concept_title", "") or unit.get("title", "") or "").strip()
        section_title = str(unit.get("section_title", "") or "").strip()
        body = self._clean_generated_text(
            self._flatten_text_field(unit.get("body") or unit.get("content") or "").strip(),
            preserve_paragraphs=teaching_mode,
        )
        list_items = [
            self._clean_generated_text(self._flatten_text_field(item))
            for item in (unit.get("list_items") or [])
            if self._flatten_text_field(item).strip()
        ]
        note = self._clean_generated_text(
            self._flatten_text_field(unit.get("note", "")).strip()
        )
        example = self._clean_generated_text(
            self._flatten_text_field(unit.get("example", "")).strip()
        )
        formulas = self._normalize_formulas(unit.get("formulas"))
        code_blocks = self._normalize_code_blocks(unit.get("code_blocks"))
        tables = self._normalize_tables(unit.get("tables"))
        content_type = str(unit.get("content_type", "") or "explanation").strip().lower()
        key_idea = ""
        list_ordered = False
        ai_added: list[str] = []
        example_ai = False
        if teaching_mode:
            key_idea = self._normalize_key_idea(unit.get("key_idea"))
            list_ordered = bool(list_items) and unit.get("list_ordered") is True
            raw_ai_added = unit.get("ai_added")
            if isinstance(raw_ai_added, str):
                raw_ai_added = [raw_ai_added]
            ai_added = [
                text
                for text in (
                    self._clean_generated_text(self._flatten_text_field(item).strip())
                    for item in (raw_ai_added if isinstance(raw_ai_added, list) else [])
                )
                if text
            ][:4]
        # Only the teaching prompts and the quality revision ask for example_ai, so an
        # unset flag stays False for every other note.
        example_ai = bool(example) and unit.get("example_ai") is True
        # Off-topic/unrelated-discussion content is explicitly a non-course
        # aside -- it must never carry a slide reference, regardless of what
        # the model declared or what the keyword-overlap heuristic below
        # would otherwise match it to (short filler phrases like "thanks" or
        # "music" can coincidentally overlap with slide keywords).
        if content_type == "off-topic":
            slide_pages: list[int] = []
        else:
            declared_slide_pages = [
                int(page_number)
                for page_number in (unit.get("slide_pages") or [])
                if str(page_number).strip().isdigit()
            ]
            slide_pages = self._refine_unit_slide_pages(
                concept_title=concept_title,
                section_title=section_title,
                body=body,
                list_items=list_items,
                note=note,
                example=example,
                declared_slide_pages=declared_slide_pages,
                relevant_slide_pages=relevant_slide_pages,
            )
        return {
            "concept_title": concept_title or section_title or f"concept-{batch_index}-{order_in_batch}",
            "section_title": section_title,
            "content_type": content_type,
            "body": body,
            "formulas": formulas,
            "code_blocks": code_blocks,
            "tables": tables,
            "list_items": list_items,
            "key_idea": key_idea,
            "list_ordered": list_ordered,
            "ai_added": ai_added,
            "example_ai": example_ai,
            "note": note,
            "example": example,
            "slide_pages": sorted(set(slide_pages))[:3],
            "batch_index": batch_index,
            "chunk_indexes": list(batch.get("chunk_indexes", [])),
            "start_ts": float(batch.get("start_ts", 0.0) or 0.0),
            "end_ts": float(batch.get("end_ts", 0.0) or 0.0),
            # Teaching mode orders units by the planned study sequence, so a
            # topic's units must never interleave with the next topic's.
            "order_index": (
                (batch_index * 10000) + order_in_batch
                if teaching_mode
                else (batch_index * 100) + order_in_batch
            ),
            "normalized_key": self._concept_key(concept_title or section_title or body),
            "source_text_hash": str(unit.get("source_text_hash", "") or self._text_hash(str(batch.get("text", "") or ""))),
            "grounding": dict(unit.get("grounding", {}) or {}),
        }

    _KEY_IDEA_MAX_CHARS = 240

    def _normalize_key_idea(self, value: Any) -> str:
        """One plain sentence stating a unit's core idea (v4 only).

        Capped so a runaway answer cannot turn the highlight into a paragraph;
        a longer value is cut at its first sentence, or dropped if that is
        still too long.
        """
        text = self._clean_generated_text(self._flatten_text_field(value).strip())
        if len(text) <= self._KEY_IDEA_MAX_CHARS:
            return text
        first_sentence = re.split(r"(?<=[\.\!\؟\?])\s+", text, maxsplit=1)[0].strip()
        return first_sentence if len(first_sentence) <= self._KEY_IDEA_MAX_CHARS else ""

    def _refine_unit_slide_pages(
        self,
        *,
        concept_title: str,
        section_title: str,
        body: str,
        list_items: list[str],
        note: str,
        example: str,
        declared_slide_pages: list[int],
        relevant_slide_pages: list[dict[str, Any]],
    ) -> list[int]:
        content_text = " ".join(
            part
            for part in [
                concept_title,
                section_title,
                body,
                " ".join(list_items),
                note,
                example,
            ]
            if part
        ).strip()
        if not content_text:
            return []
        content_tokens = self._keyword_set(content_text)
        scored: list[tuple[float, int]] = []
        relevant_by_page = {
            int(page.get("page_number") or 0): page
            for page in relevant_slide_pages
            if int(page.get("page_number") or 0) > 0
        }
        candidate_numbers = sorted(set(declared_slide_pages) | set(relevant_by_page.keys()))
        for page_number in candidate_numbers:
            page = relevant_by_page.get(page_number)
            if page is None:
                continue
            page_text = " ".join(
                [
                    str(page.get("text", "") or ""),
                    " ".join(page.get("heading_lines", []) or []),
                    " ".join(page.get("bullet_lines", []) or []),
                    " ".join(page.get("keywords", []) or []),
                    " ".join(page.get("candidate_concepts", []) or []),
                    " ".join(page.get("table_text", []) or []),
                    " ".join(page.get("formula_text", []) or []),
                ]
            )
            page_tokens = self._keyword_set(page_text)
            overlap = len(content_tokens & page_tokens)
            score = float(overlap)
            if page_number in declared_slide_pages:
                score += 0.8
            if concept_title and concept_title in (page.get("heading_lines", []) or []):
                score += 1.2
            if overlap >= 3:
                scored.append((score, page_number))
        if not scored:
            return []
        scored.sort(reverse=True)
        best_score, best_page = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else -1.0
        if best_score < 3.0:
            return []
        results = [best_page]
        if len(scored) > 1 and second_score >= 3.0 and best_score - second_score <= 1.2:
            results.append(scored[1][1])
        return sorted(set(results))[:2]

    def _merge_content_units(
        self, *, content_units: list[dict[str, Any]], chronological: bool = True
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        merged_map: dict[str, dict[str, Any]] = {}
        dropped_duplicates = 0
        merged_count = 0

        for unit in sorted(content_units, key=lambda item: (item.get("batch_index", 0), item.get("order_index", 0))):
            key = str(unit.get("normalized_key", "") or "").strip()
            if not key:
                key = self._concept_key(
                    " ".join(
                        [
                            str(unit.get("concept_title", "") or ""),
                            str(unit.get("section_title", "") or ""),
                            str(unit.get("body", "") or ""),
                        ]
                    )
                )
                unit["normalized_key"] = key
            existing = merged_map.get(key)
            if existing is None:
                current = deepcopy(unit)
                current["body_segments"] = [current["body"]] if current.get("body") else []
                merged_map[key] = current
                continue

            merged_count += 1
            if not existing.get("section_title") and unit.get("section_title"):
                existing["section_title"] = unit["section_title"]
            if not existing.get("concept_title") and unit.get("concept_title"):
                existing["concept_title"] = unit["concept_title"]
            if len(str(unit.get("concept_title", "") or "")) > len(str(existing.get("concept_title", "") or "")):
                existing["concept_title"] = unit["concept_title"]

            if unit.get("body"):
                if self._is_duplicate_text(unit["body"], existing.get("body_segments", [])):
                    dropped_duplicates += 1
                else:
                    existing.setdefault("body_segments", []).append(unit["body"])
            if unit.get("note") and not self._is_duplicate_text(unit["note"], [str(existing.get("note", "") or "")]):
                existing["note"] = self._merge_text_fragments(str(existing.get("note", "") or ""), unit["note"])
            if unit.get("example") and not self._is_duplicate_text(unit["example"], [str(existing.get("example", "") or "")]):
                existing["example"] = self._merge_text_fragments(str(existing.get("example", "") or ""), unit["example"])

            if not existing.get("key_idea") and unit.get("key_idea"):
                existing["key_idea"] = unit["key_idea"]
            for ai_text in unit.get("ai_added", []) or []:
                if not self._is_duplicate_text(ai_text, list(existing.get("ai_added", []))):
                    existing["ai_added"] = [*existing.get("ai_added", []), ai_text]
            if unit.get("example_ai") and unit.get("example"):
                existing["example_ai"] = True
            existing_items = list(existing.get("list_items", []))
            for item in unit.get("list_items", []):
                if not self._is_duplicate_text(item, existing_items):
                    existing_items.append(item)
                else:
                    dropped_duplicates += 1
            existing["list_items"] = existing_items
            existing["formulas"] = [
                *existing.get("formulas", []),
                *[f for f in unit.get("formulas", []) if f not in existing.get("formulas", [])],
            ]
            existing["code_blocks"] = [
                *existing.get("code_blocks", []),
                *[c for c in unit.get("code_blocks", []) if c not in existing.get("code_blocks", [])],
            ][:6]
            existing["tables"] = [
                *existing.get("tables", []),
                *[t for t in unit.get("tables", []) if t not in existing.get("tables", [])],
            ][:4]
            existing["slide_pages"] = sorted(set(existing.get("slide_pages", [])) | set(unit.get("slide_pages", [])))[:4]
            existing["chunk_indexes"] = sorted(set(existing.get("chunk_indexes", [])) | set(unit.get("chunk_indexes", [])))
            existing["start_ts"] = min(float(existing.get("start_ts", 0.0) or 0.0), float(unit.get("start_ts", 0.0) or 0.0))
            existing["end_ts"] = max(float(existing.get("end_ts", 0.0) or 0.0), float(unit.get("end_ts", 0.0) or 0.0))
            existing["order_index"] = min(int(existing.get("order_index", 0) or 0), int(unit.get("order_index", 0) or 0))
            existing["content_type"] = self._pick_stronger_content_type(
                existing=str(existing.get("content_type", "explanation") or "explanation"),
                incoming=str(unit.get("content_type", "explanation") or "explanation"),
            )

        if chronological:
            merged_units = sorted(
                merged_map.values(),
                key=lambda item: (item.get("start_ts", 0.0), item.get("order_index", 0)),
            )
        else:
            merged_units = sorted(merged_map.values(), key=lambda item: item.get("order_index", 0))
        for unit in merged_units:
            body_segments = [segment for segment in unit.pop("body_segments", []) if segment]
            unit["body"] = self._merge_unique_segments(
                body_segments, separator=" " if chronological else "\n\n"
            )
        return merged_units, {
            "input_unit_count": len(content_units),
            "merged_unit_count": merged_count,
            "output_unit_count": len(merged_units),
            "dropped_duplicate_units": dropped_duplicates,
        }

    def _remove_repeated_definitions_safely(
        self,
        *,
        units: list[dict[str, Any]],
        context: dict[str, Any],
        preserve_paragraphs: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        processed_units: list[dict[str, Any]] = []
        seen_definition_sentences: list[dict[str, Any]] = []
        removed_sentence_count = 0
        dropped_whole_unit_count = 0
        kept_units_with_new_value_count = 0

        for unit in (deepcopy(item) for item in units):
            fingerprint = self._definition_dedup_fingerprint(unit=unit, context=context)
            body = str(unit.get("body", "") or "")
            # Teaching-mode bodies carry blank-line paragraph breaks, so the
            # filter runs inside each paragraph and the breaks survive it.
            source_paragraphs = (
                [part for part in re.split(r"\n\s*\n", body) if part.strip()]
                if preserve_paragraphs
                else [body]
            )
            original_sentences: list[str] = []
            kept_paragraphs: list[str] = []
            removed_from_unit = 0

            for paragraph in source_paragraphs:
                paragraph_sentences = self._split_into_sentences(paragraph)
                original_sentences.extend(paragraph_sentences)
                kept_sentences: list[str] = []
                for sentence in paragraph_sentences:
                    if not self._is_definition_like_sentence(sentence):
                        kept_sentences.append(sentence)
                        continue
                    if self._sentence_is_duplicate_definition(
                        sentence=sentence,
                        fingerprint=fingerprint,
                        previous_definitions=seen_definition_sentences,
                    ):
                        removed_sentence_count += 1
                        removed_from_unit += 1
                        continue
                    kept_sentences.append(sentence)
                if kept_sentences:
                    kept_paragraphs.append(" ".join(kept_sentences).strip())

            cleaned_unit = deepcopy(unit)
            cleaned_unit["body"] = (
                "\n\n".join(part for part in kept_paragraphs if part).strip()
                if preserve_paragraphs
                else " ".join(kept_paragraphs).strip()
            )
            has_new_value = self._unit_has_new_educational_value(
                unit=cleaned_unit,
                fingerprint=fingerprint,
                previous_units=processed_units,
                removed_definition_sentence_count=removed_from_unit,
            )
            if removed_from_unit and has_new_value:
                kept_units_with_new_value_count += 1

            no_remaining_body = not str(cleaned_unit.get("body", "") or "").strip()
            only_duplicate_definitions = original_sentences and removed_from_unit == len(
                original_sentences
            )
            if no_remaining_body and only_duplicate_definitions and not has_new_value:
                merge_target = self._find_metadata_merge_target(
                    fingerprint=fingerprint,
                    processed_units=processed_units,
                )
                if merge_target is not None:
                    self._merge_metadata_without_losing_content(
                        target=merge_target,
                        incoming=unit,
                    )
                dropped_whole_unit_count += 1
                continue

            processed_units.append(cleaned_unit)
            for sentence in self._split_into_sentences(str(cleaned_unit.get("body", "") or "")):
                if self._is_definition_like_sentence(sentence):
                    seen_definition_sentences.append(
                        {
                            "sentence": sentence,
                            "fingerprint": self._definition_dedup_fingerprint(
                                unit=cleaned_unit,
                                context=context,
                            ),
                        }
                    )

        return processed_units, {
            "input_unit_count": len(units),
            "output_unit_count": len(processed_units),
            "removed_duplicate_definition_sentence_count": removed_sentence_count,
            "dropped_whole_unit_count": dropped_whole_unit_count,
            "kept_units_with_new_value_count": kept_units_with_new_value_count,
            "strategy": "sentence_level_definition_dedup_conservative",
        }

    _FIELD_REPEAT_MIN_KEYWORDS = 5
    _FIELD_REPEAT_CONTAINMENT = 0.55

    def _keywords_of(self, text: str) -> set[str]:
        return self._keyword_set(re.sub(r"\[\[slide:[^\]]*\]\]", " ", str(text or "")))

    def _contained_in(self, text: str, reference: set[str]) -> bool:
        """True when almost every keyword of `text` already appears in `reference`."""
        keywords = self._keywords_of(text)
        if len(keywords) < self._FIELD_REPEAT_MIN_KEYWORDS:
            return False
        return len(keywords & reference) / len(keywords) >= self._FIELD_REPEAT_CONTAINMENT

    def _prune_sentences_contained_in(self, text: str, reference: set[str]) -> tuple[str, int]:
        paragraphs = re.split(r"\n\s*\n", str(text or ""))
        kept_paragraphs: list[str] = []
        removed = 0
        for paragraph in paragraphs:
            kept: list[str] = []
            for sentence in self._split_into_sentences(paragraph):
                if self._contained_in(sentence, reference):
                    removed += 1
                else:
                    kept.append(sentence)
            if kept:
                kept_paragraphs.append(" ".join(kept))
        return "\n\n".join(kept_paragraphs).strip(), removed

    def _remove_redundant_unit_fields(
        self, *, units: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Say each idea once inside a unit: list, then note, then example.

        The list is the clearest form of a procedure or enumeration, so body
        sentences it already covers are removed. A note is dropped when body and
        list already say it. An example is dropped when it only re-walks what
        the other fields state; one with its own concrete figures (two or more
        numbers found nowhere else in the unit) is a real worked example and stays.
        """
        report = {"body_sentences_removed": 0, "notes_removed": 0, "examples_removed": 0}
        result: list[dict[str, Any]] = []

        def numbers(text: str) -> set[str]:
            return set(re.findall(r"\d+(?:[.,٫]\d+)?", str(text or "")))

        for unit in units:
            cleaned = deepcopy(unit)
            if str(cleaned.get("content_type", "") or "") != "off-topic":
                list_text = " ".join(str(item) for item in cleaned.get("list_items") or [])
                if list_text.strip() and cleaned.get("body"):
                    cleaned["body"], removed = self._prune_sentences_contained_in(
                        cleaned["body"], self._keywords_of(list_text)
                    )
                    report["body_sentences_removed"] += removed
                others = " ".join([str(cleaned.get("body", "")), list_text])
                reference = self._keywords_of(others)
                note = str(cleaned.get("note", "") or "")
                if note and self._contained_in(note, reference):
                    cleaned["note"] = ""
                    report["notes_removed"] += 1
                example = str(cleaned.get("example", "") or "")
                if example:
                    others = " ".join([others, str(cleaned.get("note", ""))])
                    fresh_numbers = numbers(example) - numbers(others)
                    if self._contained_in(example, self._keywords_of(others)) and len(fresh_numbers) < 2:
                        cleaned["example"] = ""
                        cleaned["example_ai"] = False
                        report["examples_removed"] += 1
            result.append(cleaned)
        return result, report

    _REPEAT_MIN_KEYWORDS = 6
    _REPEAT_MIN_JACCARD = 0.8

    def _remove_repeated_statements(
        self, *, units: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Write each statement once when the lecturer repeated it (v2).

        Only prose (body and note) loses sentences, and only a sentence that is
        almost word-for-word an earlier one in the note: same keywords, same
        numbers and same Latin terms, so a restatement that adds a figure, a
        condition or an example is kept. Lists, formulas, tables, code and
        examples are never touched, and a unit is dropped only if nothing at
        all is left in it.
        """
        seen: list[tuple[set[str], frozenset[str], frozenset[str]]] = []
        removed_sentences = 0
        dropped_units = 0
        result: list[dict[str, Any]] = []

        def signature(sentence: str) -> tuple[set[str], frozenset[str], frozenset[str]]:
            return (
                self._keyword_set(sentence),
                frozenset(re.findall(r"\d+(?:[.,٫]\d+)?", sentence)),
                frozenset(term.casefold() for term in re.findall(r"[A-Za-z][A-Za-z0-9+#]+", sentence)),
            )

        def is_repeat(sentence: str) -> bool:
            keywords, numbers, latin = signature(sentence)
            if len(keywords) < self._REPEAT_MIN_KEYWORDS:
                return False
            for prev_keywords, prev_numbers, prev_latin in seen:
                if numbers != prev_numbers or latin != prev_latin:
                    continue
                union = len(keywords | prev_keywords)
                if union and len(keywords & prev_keywords) / union >= self._REPEAT_MIN_JACCARD:
                    return True
            return False

        def dedupe(text: str, *, keep_paragraphs: bool) -> str:
            nonlocal removed_sentences
            paragraphs = re.split(r"\n\s*\n", text) if keep_paragraphs else [text]
            kept_paragraphs: list[str] = []
            for paragraph in paragraphs:
                kept: list[str] = []
                for sentence in self._split_into_sentences(paragraph):
                    if is_repeat(sentence):
                        removed_sentences += 1
                        continue
                    kept.append(sentence)
                    seen.append(signature(sentence))
                if kept:
                    kept_paragraphs.append(" ".join(kept))
            return ("\n\n" if keep_paragraphs else " ").join(kept_paragraphs).strip()

        for unit in units:
            cleaned = deepcopy(unit)
            had_prose = bool(str(unit.get("body", "") or "").strip() or str(unit.get("note", "") or "").strip())
            cleaned["body"] = dedupe(str(unit.get("body", "") or ""), keep_paragraphs=True)
            cleaned["note"] = dedupe(str(unit.get("note", "") or ""), keep_paragraphs=False)
            still_has_content = any(
                cleaned.get(field)
                for field in (
                    "body", "note", "example", "list_items", "formulas", "code_blocks", "tables",
                    "key_idea", "ai_added",
                )
            )
            if had_prose and not still_has_content:
                dropped_units += 1
                continue
            result.append(cleaned)
        return result, {
            "removed_sentence_count": removed_sentences,
            "dropped_unit_count": dropped_units,
        }

    _MIN_OFF_TOPIC_BODY_CHARS = 20

    def _drop_junk_off_topic_units(
        self, *, units: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], int]:
        """Drop off-topic units too short to be a coherent remark.

        A one-word "music" or an "unclear audio" fragment renders as a box in
        the note that carries nothing, so it is removed deterministically
        instead of trusting the prompt alone.
        """
        kept: list[dict[str, Any]] = []
        dropped = 0
        for unit in units:
            if str(unit.get("content_type", "") or "").strip().lower() == "off-topic":
                body = str(unit.get("body", "") or "").strip()
                has_extra = bool(unit.get("list_items") or unit.get("example") or unit.get("note"))
                if len(body) < self._MIN_OFF_TOPIC_BODY_CHARS and not has_extra:
                    dropped += 1
                    continue
            kept.append(unit)
        return kept, dropped

    def _definition_dedup_fingerprint(
        self,
        *,
        unit: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        concept_title = str(unit.get("concept_title", "") or "").strip()
        section_title = str(unit.get("section_title", "") or "").strip()
        body = str(unit.get("body", "") or "").strip()
        note = str(unit.get("note", "") or "").strip()
        example = str(unit.get("example", "") or "").strip()
        list_text = " ".join(
            str(item) for item in unit.get("list_items", []) if str(item).strip()
        )
        equivalent_terms = " ".join(
            [
                self._extract_equivalent_terms(concept_title),
                self._extract_equivalent_terms(section_title),
                self._extract_equivalent_terms(body),
            ]
        )
        first_body_sentences = " ".join(self._split_into_sentences(body)[:2])
        title_tokens = self._keyword_set(" ".join([concept_title, equivalent_terms]))
        section_tokens = self._keyword_set(section_title)
        content_tokens = self._keyword_set(
            " ".join(
                [
                    concept_title,
                    section_title,
                    first_body_sentences,
                    note,
                    example,
                    list_text,
                    str(unit.get("content_type", "") or ""),
                    str(context.get("lecture", {}).get("title", "") or ""),
                ]
            )
        )
        return {
            "concept_title": concept_title,
            "section_title": section_title,
            "title_key": self._concept_key(concept_title),
            "section_key": self._concept_key(section_title),
            "title_tokens": title_tokens,
            "section_tokens": section_tokens,
            "content_tokens": content_tokens,
            "content_type": str(unit.get("content_type", "") or "").strip().lower(),
            "slide_pages": {
                int(page) for page in unit.get("slide_pages", []) if str(page).isdigit()
            },
            "chunk_indexes": {
                int(index)
                for index in unit.get("chunk_indexes", [])
                if str(index).lstrip("-").isdigit()
            },
        }

    def _find_metadata_merge_target(
        self,
        *,
        fingerprint: dict[str, Any],
        processed_units: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for candidate in reversed(processed_units):
            candidate_fingerprint = self._definition_dedup_fingerprint(
                unit=candidate,
                context={},
            )
            if self._fingerprints_indicate_same_definition_scope(
                left=fingerprint,
                right=candidate_fingerprint,
            ):
                return candidate
        return None

    def _split_into_sentences(self, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if not cleaned:
            return []
        rough_sentences = re.split(
            r"(?<=[\.\!\؟؛])\s+|\n+|(?<=\])\s+(?=[\u0600-\u06FFA-Za-z])",
            cleaned,
        )
        sentences: list[str] = []
        for sentence in rough_sentences:
            stripped = sentence.strip()
            if not stripped:
                continue
            if len(stripped) > 520:
                clauses = [
                    part.strip()
                    for part in re.split(r"(?<=،)\s+", stripped)
                    if part.strip()
                ]
                sentences.extend(clauses if len(clauses) > 1 else [stripped])
            else:
                sentences.append(stripped)
        return sentences

    def _is_definition_like_sentence(self, sentence: str) -> bool:
        text = str(sentence or "").strip().lower()
        if not text:
            return False
        patterns = [
            r"\b(is|are|means|refers to|defined as|definition|concept)\b",
            r"(عبارت است از|تعریف می‌شود|تعریف میشود|یعنی|به معنای|منظور از|"
            r"گفته می‌شود|گفته میشود)",
            r"(چیست|هستند|است که|حالتی است|مفهومی است|فرایندی است|روشی است|"
            r"مدلی است|تکنیکی است|معیاری است)",
        ]
        return any(re.search(pattern, text) for pattern in patterns)

    def _sentence_is_duplicate_definition(
        self,
        *,
        sentence: str,
        fingerprint: dict[str, Any],
        previous_definitions: list[dict[str, Any]],
    ) -> bool:
        if not self._is_definition_like_sentence(sentence):
            return False
        for previous in previous_definitions:
            previous_sentence = str(previous.get("sentence", "") or "")
            previous_fingerprint = dict(previous.get("fingerprint", {}) or {})
            if not self._fingerprints_indicate_same_definition_scope(
                left=fingerprint,
                right=previous_fingerprint,
            ):
                continue
            similarity = self._sentence_similarity(sentence, previous_sentence)
            sentence_tokens = self._keyword_set(sentence)
            previous_tokens = self._keyword_set(previous_sentence)
            containment = self._overlap_ratio(sentence_tokens, previous_tokens)
            if similarity >= 0.78:
                return True
            if similarity >= 0.62 and containment >= 0.72:
                return True
            if similarity >= 0.48 and containment >= 0.54:
                return self._definition_sentences_share_core_terms(
                    sentence_tokens=sentence_tokens,
                    previous_tokens=previous_tokens,
                    fingerprint=fingerprint,
                )
        return False

    def _fingerprints_indicate_same_definition_scope(
        self,
        *,
        left: dict[str, Any],
        right: dict[str, Any],
    ) -> bool:
        left_title = set(left.get("title_tokens", set()))
        right_title = set(right.get("title_tokens", set()))
        left_content = set(left.get("content_tokens", set()))
        right_content = set(right.get("content_tokens", set()))
        if not left_title or not right_title:
            return False
        same_title_key = bool(left.get("title_key") and left.get("title_key") == right.get("title_key"))
        title_overlap = self._overlap_ratio(left_title, right_title)
        content_overlap = self._overlap_ratio(left_content, right_content)
        same_section = bool(
            left.get("section_key")
            and right.get("section_key")
            and left.get("section_key") == right.get("section_key")
        )
        slide_overlap = bool(set(left.get("slide_pages", set())) & set(right.get("slide_pages", set())))
        chunk_overlap = bool(
            set(left.get("chunk_indexes", set())) & set(right.get("chunk_indexes", set()))
        )
        if same_title_key and (content_overlap >= 0.38 or same_section or slide_overlap or chunk_overlap):
            return True
        if title_overlap >= 0.86 and content_overlap >= 0.52:
            return True
        if title_overlap >= 0.72 and content_overlap >= 0.62 and (same_section or slide_overlap):
            return True
        return False

    def _definition_sentences_share_core_terms(
        self,
        *,
        sentence_tokens: set[str],
        previous_tokens: set[str],
        fingerprint: dict[str, Any],
    ) -> bool:
        title_tokens = set(fingerprint.get("title_tokens", set()))
        shared_title_tokens = title_tokens & sentence_tokens & previous_tokens
        shared_non_title_tokens = (sentence_tokens & previous_tokens) - title_tokens
        return bool(shared_title_tokens) and len(shared_non_title_tokens) >= 3

    def _unit_has_new_educational_value(
        self,
        *,
        unit: dict[str, Any],
        fingerprint: dict[str, Any],
        previous_units: list[dict[str, Any]],
        removed_definition_sentence_count: int,
    ) -> bool:
        if str(unit.get("example", "") or "").strip():
            return True
        if str(unit.get("note", "") or "").strip():
            return True
        if any(str(item or "").strip() for item in unit.get("list_items", []) or []):
            return True
        if unit.get("tables"):
            return True
        body = str(unit.get("body", "") or "").strip()
        if self._text_has_new_value_signal(body):
            return True
        previous_fingerprints = [
            self._definition_dedup_fingerprint(unit=previous, context={})
            for previous in previous_units
        ]
        matching_previous = [
            previous
            for previous in previous_fingerprints
            if self._fingerprints_indicate_same_definition_scope(left=fingerprint, right=previous)
        ]
        if not matching_previous:
            return bool(body) or removed_definition_sentence_count == 0
        if any(
            fingerprint.get("section_key") != previous.get("section_key")
            for previous in matching_previous
        ):
            return True
        if any(
            fingerprint.get("content_type") != previous.get("content_type")
            for previous in matching_previous
        ):
            return True
        previous_slide_pages: set[int] = set()
        for previous in matching_previous:
            previous_slide_pages.update(previous.get("slide_pages", set()))
        if set(fingerprint.get("slide_pages", set())) - previous_slide_pages:
            return True
        if body:
            previous_text = " ".join(
                self._unit_educational_text(previous) for previous in previous_units
            )
            novel_tokens = self._keyword_set(body) - self._keyword_set(previous_text)
            if len(novel_tokens) >= 10:
                return True
        return False

    def _text_has_new_value_signal(self, text: str) -> bool:
        return bool(
            re.search(
                r"(مثال|کاربرد|پیامد|نتیجه|نکته|هشدار|استثنا|محدودیت|مزیت|عیب|"
                r"نوع|انواع|دسته|طبقه|مرحله|فرآیند|فرایند|گام|معماری|طراحی|تست|"
                r"آزمون|مقایسه|تفاوت|اثر|تاثیر|تأثیر|کسب‌وکار|مهندسی|فرمول|رابطه|"
                r"جدول|اسلاید|application|example|consequence|impact|architecture|"
                r"design|testing|category|process|stage|warning|comparison|business|"
                r"engineering|formula|table)",
                str(text or ""),
                re.IGNORECASE,
            )
        )

    def _merge_metadata_without_losing_content(
        self,
        *,
        target: dict[str, Any],
        incoming: dict[str, Any],
    ) -> None:
        target["slide_pages"] = sorted(
            set(target.get("slide_pages", [])) | set(incoming.get("slide_pages", []))
        )[:4]
        target["chunk_indexes"] = sorted(
            set(target.get("chunk_indexes", [])) | set(incoming.get("chunk_indexes", []))
        )
        target["start_ts"] = min(
            float(target.get("start_ts", 0.0) or 0.0),
            float(incoming.get("start_ts", 0.0) or 0.0),
        )
        target["end_ts"] = max(
            float(target.get("end_ts", 0.0) or 0.0),
            float(incoming.get("end_ts", 0.0) or 0.0),
        )
        target["order_index"] = min(
            int(target.get("order_index", 0) or 0),
            int(incoming.get("order_index", 0) or 0),
        )

    def _repair_missing_concepts(
        self,
        *,
        lecture: Lecture,
        note: GeneratedNote,
        model_name: str,
        provider: GapGPTNoteProvider,
        context: dict[str, Any],
        merged_units: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int, dict[str, int]]:
        inventory = context.get("concept_inventory", [])
        covered_keys = {self._concept_key(str(unit.get("concept_title", "") or "")) for unit in merged_units}
        missing_candidates: list[dict[str, Any]] = []
        for item in inventory:
            if not isinstance(item, dict):
                continue
            normalized = str(item.get("normalized_title", "") or "")
            if not normalized or normalized in covered_keys:
                continue
            prominence = float(item.get("prominence", 0.0) or 0.0)
            transcript_overlap = int(item.get("transcript_overlap", 0) or 0)
            if prominence < 2.4 or transcript_overlap < 2:
                continue
            missing_candidates.append(item)

        missing_candidates = missing_candidates[:5]
        repaired_units: list[dict[str, Any]] = []
        repair_count = 0
        aggregate_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for concept in missing_candidates:
            transcript_excerpt = self._extract_transcript_evidence_for_concept(
                transcript_text=str(context.get("transcript_text", "") or ""),
                concept=concept,
            )
            relevant_pages = self._resolve_concept_slide_pages(context=context, concept=concept)
            if len(transcript_excerpt) < 60:
                continue
            self.update(
                note=note,
                status=NoteGenerationStatus.STRUCTURING,
                progress=52,
                stage_detail="REPAIRING_MISSING_CONCEPTS",
                stage_message=f"Recovering a missing concept from transcript and slides: {concept.get('title', '')}",
            )
            payload, llm_result = self._repair_missing_concept_via_model(
                lecture=lecture,
                provider=provider,
                concept=concept,
                transcript_excerpt=transcript_excerpt,
                relevant_slide_pages=relevant_pages,
                event_callback=event_callback,
            )
            units = self._extract_content_units_from_batch_payload(payload)
            for order_in_batch, unit in enumerate(units):
                repaired_units.append(
                    self._normalize_content_unit(
                        unit=unit,
                        batch={"chunk_indexes": [], "start_ts": 0.0, "end_ts": 0.0},
                        batch_index=999,
                        order_in_batch=order_in_batch,
                        relevant_slide_pages=relevant_pages,
                    )
                )
            usage = llm_result.usage
            aggregate_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
            aggregate_usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
            aggregate_usage["total_tokens"] += int(usage.get("total_tokens", 0))
            if units:
                repair_count += 1

        if repaired_units:
            merged_units, _dedup = self._merge_content_units(content_units=[*merged_units, *repaired_units])
            covered_keys = {self._concept_key(str(unit.get("concept_title", "") or "")) for unit in merged_units}

        coverage_report = {
            "inventory_size": len(inventory),
            "covered_concepts": sorted(key for key in covered_keys if key)[:120],
            "missing_concepts": [
                {
                    "title": item.get("title", ""),
                    "page_numbers": item.get("page_numbers", []),
                    "prominence": item.get("prominence", 0),
                }
                for item in missing_candidates
                if str(item.get("normalized_title", "") or "") not in covered_keys
            ],
            "repaired_concepts": [
                {
                    "title": item.get("title", ""),
                    "page_numbers": item.get("page_numbers", []),
                }
                for item in missing_candidates
                if str(item.get("normalized_title", "") or "") in covered_keys
            ],
        }
        return merged_units, coverage_report, repair_count, aggregate_usage

    def _repair_transcript_alignment_units(
        self,
        *,
        lecture: Lecture,
        note: GeneratedNote,
        provider: GapGPTNoteProvider,
        context: dict[str, Any],
        merged_units: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
        aggregate_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        rewritten_count = 0
        adjusted_units: list[dict[str, Any]] = []
        transcript_text = str(context.get("transcript_text", "") or "")
        has_slides = bool(context.get("has_slides"))
        max_repairs = 4 if has_slides else 2

        for unit in merged_units:
            body = str(unit.get("body", "") or "")
            note_text = str(unit.get("note", "") or "")
            example_text = str(unit.get("example", "") or "")
            combined = " ".join([body, note_text, example_text]).strip()
            if not combined:
                adjusted_units.append(unit)
                continue

            suspicious_noise = self._is_suspicious_noisy_unit(
                unit=unit,
                context=context,
            )
            if has_slides:
                needs_rewrite = self._persian_ratio(combined) < 0.35 or self._transcript_overlap_ratio(
                    text=combined,
                    transcript_text=transcript_text,
                ) < 0.08 or suspicious_noise
            else:
                needs_rewrite = self._persian_ratio(combined) < 0.28 or suspicious_noise
            if not needs_rewrite:
                adjusted_units.append(unit)
                continue
            if rewritten_count >= max_repairs:
                adjusted_units.append(unit)
                continue

            transcript_excerpt = self._extract_transcript_excerpt_for_unit(
                transcript_text=transcript_text,
                unit=unit,
            )
            if len(transcript_excerpt) < 80:
                adjusted_units.append(unit)
                continue

            relevant_pages = self._resolve_unit_slide_pages(context=context, unit=unit)
            self.update(
                note=note,
                status=NoteGenerationStatus.STRUCTURING,
                progress=51,
                stage_detail="ALIGNING_TO_TRANSCRIPT",
                stage_message=f"Rewriting one concept to stay faithful to the transcript: {unit.get('concept_title', '')}",
            )
            payload, llm_result = self._rewrite_unit_from_transcript(
                lecture=lecture,
                provider=provider,
                unit=unit,
                transcript_excerpt=transcript_excerpt,
                relevant_slide_pages=relevant_pages,
                event_callback=event_callback,
            )
            rewritten_units = self._extract_content_units_from_batch_payload(payload)
            if rewritten_units:
                normalized = self._normalize_content_unit(
                    unit=rewritten_units[0],
                    batch={
                        "chunk_indexes": unit.get("chunk_indexes", []),
                        "start_ts": unit.get("start_ts", 0.0),
                        "end_ts": unit.get("end_ts", 0.0),
                    },
                    batch_index=int(unit.get("batch_index", 0) or 0),
                    order_in_batch=0,
                    relevant_slide_pages=relevant_pages,
                )
                # The v2 structure-first pipeline forces section_title and a
                # topic_anchor/topic_order onto every unit of a topic so the
                # table-of-contents links to an exact-matching section in
                # the body. A transcript-alignment rewrite regenerates the
                # unit from scratch and knows nothing about that convention
                # -- without restoring these fields, the rewritten unit
                # turned into an orphaned, unlinked section headed by
                # whatever title the rewrite happened to invent instead of
                # the topic's real one.
                if unit.get("topic_anchor"):
                    normalized["section_title"] = unit.get("section_title", "")
                    normalized["topic_anchor"] = unit["topic_anchor"]
                    normalized["topic_order"] = unit.get("topic_order")
                adjusted_units.append(normalized)
                rewritten_count += 1
                usage = llm_result.usage
                aggregate_usage["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
                aggregate_usage["completion_tokens"] += int(usage.get("completion_tokens", 0))
                aggregate_usage["total_tokens"] += int(usage.get("total_tokens", 0))
            else:
                adjusted_units.append(unit)

        return adjusted_units, {"rewritten_unit_count": rewritten_count}, aggregate_usage

    def _verify_unit_formulas(
        self,
        *,
        lecture: Lecture,
        provider: GapGPTNoteProvider,
        units: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
        """One batched LLM pass that checks every unit formula for notation and factual errors.

        Fails open: any provider/parse problem keeps the original formulas.
        """
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        candidates = [
            (index, unit)
            for index, unit in enumerate(units)
            if isinstance(unit.get("formulas"), list) and unit["formulas"]
        ]
        report = {"checked_formula_count": 0, "corrected_formula_count": 0}
        if not candidates:
            return units, report, usage_total
        system_prompt = (
            "You are a meticulous mathematics and engineering proofreader. You receive formulas "
            "taken from lecture notes, each with the concept it belongs to. Fix (1) notation "
            "errors: subscripts/superscripts that swallowed neighbouring variables (C_{3n}^{2} "
            "for C_3 n^2), missing braces, wrong operators, broken LaTeX; and (2) clear "
            "mathematical or factual errors that the concept context shows (wrong exponent, "
            "wrong sign, wrong term, wrong standard formula). Change a formula only when you "
            "are confident it is wrong; otherwise return it exactly as given. Never add, drop "
            "or reorder formulas. Write corrected formulas as compact LaTeX-lite without dollar "
            "signs, with every sub/superscript braced and carrying only its own index "
            "(C_{3} n^{2}). Return JSON only."
        )
        batch_size = 12
        chunks = [
            candidates[start : start + batch_size]
            for start in range(0, len(candidates), batch_size)
        ]

        def verify_chunk(chunk: list[tuple[int, dict[str, Any]]], *, event_callback: Any | None = None) -> Any:
            payload_items = [
                {
                    "unit": index,
                    "concept": str(unit.get("concept_title", "") or "")[:160],
                    "context": " ".join(str(unit.get("body", "") or "").split())[:500],
                    "formulas": unit["formulas"],
                }
                for index, unit in chunk
            ]
            user_prompt = (
                f"Course: {lecture.course_name}\n"
                f"Formulas to verify:\n{json.dumps(payload_items, ensure_ascii=False)}\n"
                'Return JSON {"units": [{"unit": <same number>, "formulas": [<same count, same '
                "order, corrected or unchanged>]}]}"
            )
            # Fails open per batch: one bad answer keeps that batch's original
            # formulas instead of discarding every other batch's corrections.
            try:
                signature = inspect.signature(provider.complete_json)
                if "event_callback" in signature.parameters:
                    return provider.complete_json(
                        system_prompt=system_prompt, user_prompt=user_prompt, event_callback=event_callback
                    )
                return provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)
            except Exception:
                logger.warning(
                    "Formula verification batch failed; keeping its original formulas.", exc_info=True
                )
                return None

        # Each batch carries its own formulas and nothing one answer says changes
        # another batch's prompt, so the calls run side by side; answers are
        # applied below in batch order.
        outcomes = self._run_independent_calls(
            jobs=[partial(verify_chunk, chunk) for chunk in chunks],
            parallelism=self._effective_structure_parallelism(writable_topics=len(chunks)),
            event_callback=event_callback,
        )

        for chunk, outcome in zip(chunks, outcomes, strict=True):
            if outcome is None:
                continue
            payload, llm_result = outcome
            for key in usage_total:
                usage_total[key] += int(getattr(llm_result, "usage", {}).get(key, 0) or 0)
            answers = payload.get("units") if isinstance(payload, dict) else None
            if not isinstance(answers, list):
                continue
            by_index = {index: unit for index, unit in chunk}
            for answer in answers:
                if not isinstance(answer, dict):
                    continue
                try:
                    unit = by_index.get(int(answer.get("unit")))
                except (TypeError, ValueError):
                    continue
                fixed = answer.get("formulas")
                if unit is None or not isinstance(fixed, list) or len(fixed) != len(unit["formulas"]):
                    continue
                cleaned = self._normalize_formulas([str(item) for item in fixed])
                if len(cleaned) != len(unit["formulas"]):
                    continue
                report["checked_formula_count"] += len(cleaned)
                report["corrected_formula_count"] += sum(
                    1 for old, new in zip(unit["formulas"], cleaned, strict=True) if old != new
                )
                unit["formulas"] = cleaned
        return units, report, usage_total

    def _rewrite_unit_from_transcript(
        self,
        *,
        lecture: Lecture,
        provider: GapGPTNoteProvider,
        unit: dict[str, Any],
        transcript_excerpt: str,
        relevant_slide_pages: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], Any]:
        rewrite_detail_rule = (
            "Keep the repaired unit concise: retain only its essential definition, result, formula, or exam-relevant point. "
            if lecture.note_detail_mode == "summarized"
            else "Preserve all source-backed teaching detail needed to understand this concept. "
        )
        system_prompt = (
            "You rewrite one note concept so it becomes faithful to a lecture transcript. "
            f"The final note language must be {language_label(lecture.output_language, locale='en')} ({lecture.output_language}). "
            "Transcript is primary. Slides are only support for fixing unclear terms and attaching references. "
            "The transcript is machine-generated and can mishear a technical term (especially short "
            "spoken notation like O(n), O(n^2), Omega, Theta) as an unrelated ordinary word. "
            "When a transcript word does not fit its technical context and slide evidence clearly "
            "implies the intended term, correct it silently; never quote a suspected "
            "mis-transcription as if it were intentional. "
            "Do not add textbook definitions, examples, or explanations that are not in the transcript excerpt or relevant slides. "
            "For worked examples, combine setup, intermediate speech, board references, and conclusion across the excerpt; use exact slide formulas when available. "
            "If unseen board values are not recoverable, use a labeled symbolic reconstruction rather than inventing numbers. "
            f"{rewrite_detail_rule}"
            "All output must use the selected output language. Return JSON only."
        )
        user_prompt = (
            f"Lecture title: {lecture.title}\n"
            f"Course name: {lecture.course_name}\n"
            f"Concept title: {unit.get('concept_title', '')}\n"
            f"Current weak unit content:\n{json.dumps(unit, ensure_ascii=False)}\n"
            f"Transcript excerpt:\n{transcript_excerpt}\n"
            f"Relevant slide pages:\n{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            'Return JSON with keys: document_title, units\n'
            "- units should contain exactly one concept unit\n"
            "- write the body, note, example, and list_items in the selected output language\n"
            "- do not output English paragraphs copied from slides\n"
            "- use slides only for terminology repair, exact technical labels, exact SQL/query text when present, and correct references\n"
            "- if the transcript excerpt only mentions a term without explaining it, keep it brief and do not invent a definition\n"
            "- if the current weak unit contains corrupted words from the transcript, rewrite them into the most plausible academic wording using the transcript first and the slides second\n"
            "- if the concept includes SQL or command-like content, preserve the exact command names and identifiers in readable form instead of broken phonetic substitutions\n"
            "- an example must be self-contained and include its supported reasoning/result; do not return only the professor's board-reference phrase\n"
            "- body, note, and example must each be a single plain prose string, never a JSON "
            "object or array with sub-fields\n"
        )
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            return provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt, event_callback=event_callback)
        return provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)

    def _regenerate_sparse_content_unit_batch(
        self,
        *,
        lecture: Lecture,
        context: dict[str, Any],
        provider: GapGPTNoteProvider,
        batch: dict[str, Any],
        batch_index: int,
        total_batches: int,
        relevant_slide_pages: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], Any]:
        minimum_units = self._minimum_unit_count_for_batch(
            str(batch.get("text", "") or ""),
            context=context,
        )
        summarized_mode = self._note_detail_mode(context=context) == "summarized"
        retry_reason = (
            "the previous response contained no usable summary unit"
            if summarized_mode
            else "the previous note output was too compressed"
        )
        retry_scope_rule = (
            "Return a concise summary of the major points in this batch; do not restore minor details or repetition. "
            if summarized_mode
            else "Do not summarize the whole batch or collapse multiple explained concepts into one short paragraph. "
        )
        system_prompt = (
            f"You are retrying one lecture batch because {retry_reason}. "
            "You must stay transcript-first, write in the selected output language, and recover only concepts actually explained in this batch. "
            f"{self._language_instruction(context=context)} "
            f"{self._detail_mode_instruction(context=context)} "
            "Do not add external textbook definitions, examples, or explanations. "
            f"{retry_scope_rule}"
            "Use slides only to repair unclear terms and references. Return JSON only."
        )
        user_prompt = (
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"You are retrying part {batch_index} of {total_batches} because {retry_reason}.\n"
            f"Relevant slide pages:\n{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            'Return JSON with keys: document_title, units\n'
            f"- Produce up to {minimum_units} or more concept units only when the transcript batch genuinely contains that many distinct explained concepts\n"
            "- Each concept unit must preserve substantial academic detail from the transcript, not generic prior knowledge\n"
            "- Keep everything in the selected output language except terms the lecturer said in English or another language, which stay in their original spelling\n"
            "- Transcript is primary; slides may only repair unclear transcript wording and references\n"
            "- The transcript is machine-generated and can mishear short technical notation (e.g. "
            "O(n), O(n^2), Omega, Theta) as an unrelated ordinary word; when a word does not fit "
            "its technical context and slide evidence implies the intended term, correct it "
            "silently instead of quoting the mis-transcription\n"
            "- Do not define or expand terms unless the professor or provided slides explain them\n"
            "- Do not output a final lecture summary for this batch\n"
            "- Preserve examples, comparisons, cautions, steps, mappings, formulas, and query logic when present\n"
            "- Reassemble each worked example from its full local sequence. When board-only values are absent, use a clearly labeled symbolic reconstruction and never fabricate exact numbers\n"
            "- body, note, and example must each be a single plain prose string, never a JSON "
            "object or array with sub-fields\n"
            f"Transcript part text:\n{batch['text']}"
        )
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            return provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        return provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)

    def _extract_transcript_excerpt_for_unit(self, *, transcript_text: str, unit: dict[str, Any]) -> str:
        concept = {
            "tokens": list(self._keyword_set(" ".join([
                str(unit.get("concept_title", "") or ""),
                str(unit.get("section_title", "") or ""),
                str(unit.get("body", "") or ""),
            ])))[:10]
        }
        return self._extract_transcript_evidence_for_concept(transcript_text=transcript_text, concept=concept)

    def _resolve_unit_slide_pages(self, *, context: dict[str, Any], unit: dict[str, Any]) -> list[dict[str, Any]]:
        desired = {int(page) for page in unit.get("slide_pages", []) if str(page).isdigit()}
        slide_pages = context.get("slide_context", {}).get("pages", [])
        return [page for page in slide_pages if int(page.get("page_number", 0) or 0) in desired]

    def _is_suspicious_noisy_unit(self, *, unit: dict[str, Any], context: dict[str, Any]) -> bool:
        combined = " ".join(
            [
                str(unit.get("concept_title", "") or ""),
                str(unit.get("section_title", "") or ""),
                str(unit.get("body", "") or ""),
                str(unit.get("note", "") or ""),
                str(unit.get("example", "") or ""),
                " ".join(str(item) for item in unit.get("list_items", []) if str(item).strip()),
            ]
        ).strip()
        if not combined:
            return False
        latin_glosses = re.findall(r"\([A-Za-z][^)]{0,30}\)", combined)
        suspicious_db_context = bool(
            re.search(r"\b(select|insert|update|delete|alter|join|where|from|sql)\b", combined, re.IGNORECASE)
            or any(keyword in str(context.get("lecture", {}).get("course_name", "")).lower() for keyword in ["database", "db", "sql"])
            or "دیتابیس" in str(context.get("lecture", {}).get("course_name", "") or "")
            or "پایگاه" in str(context.get("lecture", {}).get("course_name", "") or "")
        )
        phonetic_noise = len(re.findall(r"\b(?:rice|pool|burn|step|class|awali|branch|roleid|managerid|pid)\b", combined, re.IGNORECASE))
        return len(latin_glosses) >= 3 or (suspicious_db_context and phonetic_noise >= 2)

    def _repair_missing_concept_via_model(
        self,
        *,
        lecture: Lecture,
        provider: GapGPTNoteProvider,
        concept: dict[str, Any],
        transcript_excerpt: str,
        relevant_slide_pages: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], Any]:
        system_prompt = (
            "You recover one missing lecture concept for source-grounded study notes. "
            f"The final note language must be {language_label(lecture.output_language, locale='en')} ({lecture.output_language}). "
            "Transcript is primary. Use slides only to disambiguate or complete unclear terminology. "
            "The transcript is machine-generated and can mishear short technical notation (e.g. "
            "O(n), O(n^2), Omega, Theta) as an unrelated ordinary word; when a word does not fit "
            "its technical context and slide evidence implies the intended term, correct it "
            "silently instead of quoting the mis-transcription. "
            "Do not add external textbook definitions, examples, or explanations. "
            "Return JSON only."
        )
        user_prompt = (
            f"Lecture title: {lecture.title}\n"
            f"Course name: {lecture.course_name}\n"
            f"Missing concept title: {concept.get('title', '')}\n"
            f"Transcript evidence:\n{transcript_excerpt or 'No strong transcript excerpt was recovered.'}\n"
            f"Relevant slide pages:\n{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            'Return JSON with keys: document_title, units\n'
            "- units should usually contain 1 concept unit and must not repeat other concepts\n"
            "- each unit must use keys: concept_title, section_title, content_type, body, formulas, code_blocks, tables, list_items, note, example, slide_pages\n"
            "- preserve only this missing concept and its necessary explanation, not the whole lecture\n"
            "- do not invent unrelated content that is not supported by transcript or slides\n"
            "- if transcript evidence is too thin to explain the concept, keep the unit brief instead of inventing a definition\n"
            "- body, note, and example must each be a single plain prose string, never a JSON "
            "object or array with sub-fields\n"
        )
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            return provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        return provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)

    def _resolve_concept_slide_pages(self, *, context: dict[str, Any], concept: dict[str, Any]) -> list[dict[str, Any]]:
        desired_pages = {int(page) for page in concept.get("page_numbers", []) if str(page).isdigit()}
        slide_pages = context.get("slide_context", {}).get("pages", [])
        resolved = [page for page in slide_pages if int(page.get("page_number", 0) or 0) in desired_pages]
        if resolved:
            return resolved
        fallback_query = " ".join([str(concept.get("title", "") or ""), *concept.get("tokens", [])])
        return self._retrieve_relevant_slide_pages(context=context, text=fallback_query, top_k=3)

    def _extract_transcript_evidence_for_concept(self, *, transcript_text: str, concept: dict[str, Any]) -> str:
        if not transcript_text.strip():
            return ""
        sentences = [sentence.strip() for sentence in re.split(r"(?<=[\.\!\؟])\s+|\n+", transcript_text) if sentence.strip()]
        concept_tokens = {token for token in concept.get("tokens", []) if len(str(token)) >= 3}
        best_sentences: list[str] = []
        for sentence in sentences:
            sentence_tokens = self._keyword_set(sentence)
            if concept_tokens and len(sentence_tokens & concept_tokens) == 0:
                continue
            best_sentences.append(sentence)
            if len(best_sentences) >= 4:
                break
        return " ".join(best_sentences[:4]).strip()

    def _compact_summary_text(self, value: Any, *, max_chars: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(text) <= max_chars:
            return text
        sentences = self._split_note_sentences(text)
        selected: list[str] = []
        selected_chars = 0
        for sentence in sentences:
            addition = len(sentence) + (1 if selected else 0)
            if selected_chars + addition > max_chars:
                break
            selected.append(sentence)
            selected_chars += addition
        if selected:
            return " ".join(selected).strip()
        clipped = text[:max_chars].rsplit(" ", 1)[0].strip()
        return clipped or text[:max_chars].strip()

    # A study note is a selection, so its size is decided before any writing
    # happens rather than discovered afterwards. Core material earns several
    # times the room of a passing mention.
    _TEACHING_IMPORTANCE_WEIGHTS = {
        "core": 3.0,
        "supporting": 2.0,
        "mention": 0.4,
        "off_topic": 0.2,
    }
    # A topic is never asked to write more than this share of its own source:
    # a target above what the excerpts contain can only be met by padding.
    _TEACHING_MAX_TOPIC_SOURCE_SHARE = 0.8
    # Persian and English both average close to this once spaces are counted,
    # and it only has to be good enough to express a budget to the model in
    # words as well as characters.
    _TEACHING_CHARS_PER_WORD = 6

    def _teaching_length_budget(self, *, context: dict[str, Any]) -> dict[str, Any]:
        """Target size of the whole v3 note, derived from the transcript length.

        Deliberately deterministic rather than left to the model: the overshoot
        this fixes came from asking the model to decide how much to write while
        also telling it nothing may be left out.
        """
        source_chars = len(str(context.get("transcript_text", "") or ""))
        summarized = self._note_detail_mode(context=context) == "summarized"
        if summarized:
            ratio = float(settings.NOTE_V3_SUMMARIZED_SOURCE_RATIO)
            ceiling = int(settings.NOTE_V3_MAX_SUMMARIZED_NOTE_CHARS)
        else:
            ratio = float(settings.NOTE_V3_DETAILED_SOURCE_RATIO)
            ceiling = int(settings.NOTE_V3_MAX_NOTE_CHARS)
        floor = min(int(settings.NOTE_V3_MIN_NOTE_CHARS), ceiling)
        # A transcript shorter than the floor cannot justify padding up to it.
        target_chars = max(
            min(int(source_chars * ratio), ceiling),
            min(floor, source_chars) if source_chars else floor,
        )
        return {
            "source_chars": source_chars,
            "ratio": ratio,
            "ratio_percent": int(round(ratio * 100)),
            "target_chars": target_chars,
            "target_words": max(1, target_chars // self._TEACHING_CHARS_PER_WORD),
            "detail_mode": "summarized" if summarized else "detailed",
            "floor_chars": floor,
            "ceiling_chars": ceiling,
        }

    def _allocate_teaching_budget(
        self, *, context: dict[str, Any], topics: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Split the note's target size across topics by importance and length.

        Weighting by importance alone would give a long core topic the same
        room as a short one, so the source share acts as a tie-breaker inside a
        tier.
        """
        budget = self._teaching_length_budget(context=context)
        chunk_chars_by_index = {
            int(chunk.get("chunk_index", 0)): len(str(chunk.get("text", "") or ""))
            for chunk in context.get("transcript_chunks", [])
        }
        weights: dict[str, float] = {}
        for topic in topics:
            importance = self._normalize_topic_importance(topic.get("importance"))
            source_chars = sum(
                chunk_chars_by_index.get(int(idx), 0)
                for idx in topic.get("transcript_chunk_indexes", [])
            )
            weights[str(topic["topic_id"])] = self._TEACHING_IMPORTANCE_WEIGHTS[importance] * (
                1.0 + (source_chars / max(1, budget["source_chars"]))
            )
        total_weight = sum(weights.values()) or 1.0
        allocations: dict[str, dict[str, Any]] = {}
        for topic in topics:
            topic_id = str(topic["topic_id"])
            importance = self._normalize_topic_importance(topic.get("importance"))
            share = weights[topic_id] / total_weight
            # Every topic gets enough room for a real sentence or two even when
            # its share rounds to almost nothing.
            topic_chars = max(220, int(budget["target_chars"] * share))
            topic_source_chars = sum(
                chunk_chars_by_index.get(int(idx), 0)
                for idx in topic.get("transcript_chunk_indexes", [])
            )
            if topic_source_chars:
                topic_chars = max(
                    220,
                    min(topic_chars, int(topic_source_chars * self._TEACHING_MAX_TOPIC_SOURCE_SHARE)),
                )
            allocations[topic_id] = {
                "topic_id": topic_id,
                "title": str(topic.get("title", "")),
                "importance": importance,
                "share": round(share, 4),
                "source_chars": topic_source_chars,
                "target_chars": topic_chars,
                "target_words": max(1, topic_chars // self._TEACHING_CHARS_PER_WORD),
            }
        return {**budget, "topics": allocations}

    def _enforce_teaching_length_budget(
        self,
        *,
        lecture: Lecture,
        note: GeneratedNote,
        provider: GapGPTNoteProvider,
        context: dict[str, Any],
        units: list[dict[str, Any]],
        allocation: dict[str, Any],
        event_callback: Any | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, int]]:
        """Bring an over-budget v3 note back to its planned size.

        The prompts ask for the budget; this makes it bind. Over-budget topics
        are condensed by the model (hold the length, keep every core point),
        and a deterministic trim catches the cases where that call fails or
        does not cut enough -- so the guarantee never depends on one answer.
        """
        aggregate_usage: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        target_chars = int(allocation.get("target_chars", 0) or 0)
        actual_chars = sum(len(self._raw_unit_text(unit)) for unit in units)
        tolerance = float(settings.NOTE_V3_BUDGET_TOLERANCE)
        report: dict[str, Any] = {
            "enabled": bool(settings.NOTE_V3_ENFORCE_BUDGET),
            "target_chars": target_chars,
            "chars_before": actual_chars,
            "chars_after": actual_chars,
            "tolerance": tolerance,
            "overshoot_ratio": round(actual_chars / target_chars, 3) if target_chars else None,
            "condensed_topics": [],
            "fallback_topics": [],
        }
        if not target_chars:
            report["skipped_reason"] = "no_budget"
            return units, report, aggregate_usage
        if not settings.NOTE_V3_ENFORCE_BUDGET:
            report["skipped_reason"] = "disabled_by_setting"
            return units, report, aggregate_usage
        if actual_chars <= target_chars * (1 + tolerance):
            report["skipped_reason"] = "within_budget"
            return units, report, aggregate_usage

        self.update(
            note=note,
            status=NoteGenerationStatus.STRUCTURING,
            progress=52,
            stage_detail="CONDENSING_TO_BUDGET",
            stage_message="Condensing the note to its planned length.",
        )
        topic_allocations = allocation.get("topics") or {}
        units_by_topic: dict[str, list[dict[str, Any]]] = {}
        for unit in units:
            units_by_topic.setdefault(str(unit.get("topic_id", "") or ""), []).append(unit)

        condensed_by_topic: dict[str, list[dict[str, Any]]] = {}
        topic_targets: dict[str, int] = {}
        topics_to_condense: list[str] = []
        for topic_id, topic_units in units_by_topic.items():
            topic_target = int(
                (topic_allocations.get(topic_id) or {}).get("target_chars", 0) or 0
            )
            topic_chars = sum(len(self._raw_unit_text(unit)) for unit in topic_units)
            if not topic_target or topic_chars <= topic_target * (1 + tolerance):
                condensed_by_topic[topic_id] = topic_units
                continue
            topic_targets[topic_id] = topic_target
            topics_to_condense.append(topic_id)

        # Each topic is condensed on its own (nothing one answer says changes
        # another's prompt), so the calls can run side by side; results are
        # applied below in the original topic order.
        condense_started_at = time.monotonic()
        condense_outcomes = self._run_independent_calls(
            jobs=[
                partial(
                    self._condense_topic_units_to_budget,
                    lecture=lecture,
                    provider=provider,
                    context=context,
                    topic_units=units_by_topic[topic_id],
                    target_chars=topic_targets[topic_id],
                )
                for topic_id in topics_to_condense
            ],
            parallelism=self._effective_structure_parallelism(
                writable_topics=len(topics_to_condense)
            ),
            event_callback=event_callback,
        )
        report["condense_seconds"] = round(time.monotonic() - condense_started_at, 2)
        outcome_by_topic = dict(zip(topics_to_condense, condense_outcomes))

        for topic_id, topic_units in units_by_topic.items():
            if topic_id not in outcome_by_topic:
                continue
            topic_target = topic_targets[topic_id]
            topic_chars = sum(len(self._raw_unit_text(unit)) for unit in topic_units)
            rewritten, usage, condensed = outcome_by_topic[topic_id]
            for usage_key in aggregate_usage:
                aggregate_usage[usage_key] += int(usage.get(usage_key, 0) or 0)
            condensed_by_topic[topic_id] = rewritten
            after_chars = sum(len(self._raw_unit_text(unit)) for unit in rewritten)
            record = {
                "topic_id": topic_id,
                "target_chars": topic_target,
                "chars_before": topic_chars,
                "chars_after": after_chars,
                "model_condensed": condensed,
            }
            if condensed:
                report["condensed_topics"].append(record)
            if after_chars > topic_target * (1 + tolerance):
                # The model either failed or did not cut enough. Trim
                # deterministically so the budget still holds, protecting core
                # units: the mention tier collapses to one list, then
                # supporting bodies are clipped at sentence boundaries.
                condensed_by_topic[topic_id] = self._trim_units_to_budget(
                    units=rewritten, target_chars=topic_target, lecture=lecture
                )
                record["chars_after_trim"] = sum(
                    len(self._raw_unit_text(unit)) for unit in condensed_by_topic[topic_id]
                )
                report["fallback_topics"].append(record)
            restored = self._restore_lost_terms(
                lecture=lecture, before_units=topic_units, after_units=condensed_by_topic[topic_id]
            )
            if restored:
                record["restored_terms"] = restored
                if not condensed:
                    report["condensed_topics"].append(record)

        # Rebuild in the planned reading order; grouping by topic above must
        # not become the note's order.
        rebuilt = [unit for topic_units in condensed_by_topic.values() for unit in topic_units]
        rebuilt.sort(key=lambda item: int(item.get("order_index", 0) or 0))
        report["chars_after"] = sum(len(self._raw_unit_text(unit)) for unit in rebuilt)
        report["unit_count_before"] = len(units)
        report["unit_count_after"] = len(rebuilt)
        return rebuilt, report, aggregate_usage

    @staticmethod
    def _run_independent_calls(
        *,
        jobs: list[Any],
        parallelism: int,
        event_callback: Any | None,
    ) -> list[Any]:
        """Run ``job(event_callback=...)`` for each job and return results in job order.

        Sequential (and identical to calling the jobs directly) at parallelism
        1. Otherwise jobs run in worker threads; their provider events are
        queued and applied on the calling thread, which owns the database
        connection and the note object.
        """
        if parallelism <= 1 or len(jobs) <= 1:
            return [job(event_callback=event_callback) for job in jobs]
        events: queue.SimpleQueue = queue.SimpleQueue()

        def queued_event_callback(event: dict[str, Any]) -> None:
            if str(event.get("event", "")) in {"provider_attempt", "provider_slot_acquired"}:
                return
            events.put(event)

        def drain_events() -> None:
            while event_callback is not None:
                try:
                    event = events.get_nowait()
                except queue.Empty:
                    return
                try:
                    event_callback(event)
                except Exception:
                    logger.exception("note_generation_progress_update_failed")

        def run(job: Any) -> Any:
            try:
                return job(event_callback=queued_event_callback if event_callback else None)
            finally:
                connections.close_all()

        executor = ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="note-call")
        try:
            futures = [executor.submit(copy_context().run, run, job) for job in jobs]
            pending = set(futures)
            while pending:
                finished, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                drain_events()
                if any(future.exception() is not None for future in finished):
                    break
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
            drain_events()
        for future in futures:
            if not future.cancelled() and future.exception() is not None:
                raise future.exception()
        return [future.result() for future in futures]

    def _condense_topic_units_to_budget(
        self,
        *,
        lecture: Lecture,
        provider: GapGPTNoteProvider,
        context: dict[str, Any],
        topic_units: list[dict[str, Any]],
        target_chars: int,
        event_callback: Any | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
        """Ask the model to hold the length and cut what does not earn its place.

        Returns the units unchanged (and False) when the answer is unusable, so
        a bad condense answer can never lose a topic.
        """
        protected = {"core"}
        system_prompt = (
            "You are an outstanding university professor editing one section of your own study "
            "note down to its intended length. The section is already written and correct; it "
            "is simply too long for the note it belongs to. "
            f"{self._language_instruction(context=context)} "
            f"Rewrite it to fit in about {target_chars} characters, down from "
            f"{sum(len(self._raw_unit_text(unit)) for unit in topic_units)}. "
            "Hold that length and make every word earn its place. "
            "WHAT YOU MUST KEEP: every concept, definition, formula, rule, number and warning "
            "that the student needs in order to use this material, and every distinct worked "
            "example (see rule 4 for repeats). Keep them taught, not merely listed. "
            "HOW TO CUT, in this order: (1) delete repetition, restatement, and any sentence "
            "that only announces what the next one says; (2) merge several minor related points "
            "into one general statement that covers them; (3) state a secondary point in one "
            "sentence where it currently has a paragraph; (4) drop an example only when it repeats "
            "another example of the same rule, keeping the clearest one; (5) turn a long enumeration in prose into "
            "short list_items. "
            "NEVER truncate an explanation mid-thought, never leave a definition half-given, "
            "never drop a concept entirely, and never add anything new. "
            "Keep each unit's concept_title and content_type as they are; you may merge two "
            "units into one when they teach the same thing, and you may return fewer units than "
            "you were given, but never more. "
            "Write every formula in plain ASCII text; never use LaTeX or dollar signs. "
            "Keep every English term written in Latin letters (including those in parentheses "
            "after a Persian wording) exactly as it is in the section; shortening must never "
            "remove or translate one. "
            "Keep paragraph breaks as the two characters \\n\\n inside JSON strings. "
            "Return JSON only."
        )
        payload_units = [
            {
                "concept_title": str(unit.get("concept_title", "") or ""),
                "content_type": str(unit.get("content_type", "") or ""),
                "importance": str(unit.get("topic_importance", "") or ""),
                "body": str(unit.get("body", "") or ""),
                "list_items": list(unit.get("list_items", []) or []),
                "note": str(unit.get("note", "") or ""),
                "example": str(unit.get("example", "") or ""),
            }
            for unit in topic_units
        ]
        user_prompt = (
            f"Lecture metadata: {json.dumps(context['lecture'], ensure_ascii=False)}\n"
            f"Target length for this section: about {target_chars} characters.\n"
            "Return JSON with keys: units\n"
            "- units: the rewritten section, same shape as the input units "
            "(concept_title, content_type, body, list_items, note, example)\n"
            "- body, note and example are each a single plain string; list_items are plain "
            "strings\n"
            "Section to condense:\n"
            f"{json.dumps(payload_units, ensure_ascii=False)}"
        )
        try:
            complete_json_signature = inspect.signature(provider.complete_json)
            if "event_callback" in complete_json_signature.parameters:
                payload, llm_result = provider.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    event_callback=event_callback,
                )
            else:
                payload, llm_result = provider.complete_json(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )
        except PipelineError:
            logger.warning(
                "note.teaching_budget.condense_failed",
                extra={"lecture_id": str(lecture.id)},
                exc_info=True,
            )
            return topic_units, {}, False

        rewritten_raw = self._extract_content_units_from_batch_payload(payload)
        if not rewritten_raw or len(rewritten_raw) > len(topic_units):
            return topic_units, llm_result.usage, False

        rewritten: list[dict[str, Any]] = []
        for index, raw_unit in enumerate(rewritten_raw):
            # Reuse the original unit's identity (order, anchors, slide pages,
            # timestamps) so a condense only changes text, never structure.
            template = topic_units[min(index, len(topic_units) - 1)]
            condensed_unit = deepcopy(template)
            body = self._clean_generated_text(
                self._flatten_text_field(raw_unit.get("body") or "").strip(),
                preserve_paragraphs=True,
            )
            if not body and not raw_unit.get("list_items"):
                # Nothing usable came back for this unit; keep the original.
                rewritten.append(template)
                continue
            condensed_unit["body"] = body
            condensed_unit["list_items"] = [
                self._clean_generated_text(self._flatten_text_field(item))
                for item in (raw_unit.get("list_items") or [])
                if str(item).strip()
            ]
            condensed_unit["note"] = self._clean_generated_text(
                self._flatten_text_field(raw_unit.get("note") or "")
            )
            condensed_unit["example"] = self._clean_generated_text(
                self._flatten_text_field(raw_unit.get("example") or "")
            )
            rewritten.append(condensed_unit)

        # A condense is allowed to merge units under one heading, so comparing
        # titles cannot tell a merge from a deletion. What it can tell is a
        # gutting: an answer far below the length it was asked for has thrown
        # material away rather than compressed it, so keep the original and
        # let the deterministic trim cut instead.
        rewritten_chars = sum(len(self._raw_unit_text(unit)) for unit in rewritten)
        if rewritten_chars < target_chars * 0.35 and any(
            str(unit.get("topic_importance", "") or "") in protected for unit in topic_units
        ):
            return topic_units, llm_result.usage, False
        return rewritten, llm_result.usage, True

    def _restore_lost_terms(
        self,
        *,
        lecture: Lecture,
        before_units: list[dict[str, Any]],
        after_units: list[dict[str, Any]],
    ) -> list[str]:
        """Put back the names a condensed section lost, as one short list at its end."""
        teaching_after = [unit for unit in after_units if str(unit.get("content_type", "") or "") != "off-topic"]
        if not teaching_after:
            return []
        lost = lost_terms(
            [self._raw_unit_text(unit) for unit in before_units],
            [self._raw_unit_text(unit) for unit in after_units],
        )
        if not lost:
            return []
        is_persian = str(lecture.output_language or "fa").startswith("fa")
        label = "نام‌ها و اصطلاحات مطرح‌شده در این بخش" if is_persian else "Names and terms mentioned in this section"
        separator = "، " if is_persian else ", "
        last = teaching_after[-1]
        last["list_items"] = [*(last.get("list_items") or []), f"{label}: {separator.join(lost)}"]
        return lost

    def _trim_units_to_budget(
        self, *, units: list[dict[str, Any]], target_chars: int, lecture: Lecture
    ) -> list[dict[str, Any]]:
        """Deterministic last resort when a condense call will not fit the budget.

        Cuts in increasing order of harm and never shortens a core unit, so the
        worst outcome is a note that is still slightly long rather than one
        missing something it had to teach.
        """
        trimmed = [deepcopy(unit) for unit in units]

        def total() -> int:
            return sum(len(self._raw_unit_text(unit)) for unit in trimmed)

        # 1. Passing mentions become one list; they were never worth a section.
        mention_units = [
            unit
            for unit in trimmed
            if str(unit.get("topic_importance", "") or "") == "mention"
            and str(unit.get("content_type", "") or "") != "off-topic"
        ]
        if total() > target_chars and len(mention_units) > 1:
            labels = semantic_labels_for_language(lecture.output_language or "fa")
            survivor = mention_units[0]
            survivor["content_type"] = "list"
            survivor["concept_title"] = labels.get("other_topics_title") or survivor.get(
                "concept_title", ""
            )
            survivor["list_items"] = [
                item
                for unit in mention_units
                for item in (
                    [*unit.get("list_items", [])]
                    or [self._compact_summary_text(unit.get("body", ""), max_chars=180)]
                )
                if str(item).strip()
            ]
            survivor["body"] = ""
            survivor["note"] = ""
            survivor["example"] = ""
            dropped = {id(unit) for unit in mention_units[1:]}
            trimmed = [unit for unit in trimmed if id(unit) not in dropped]

        # 2. Clip non-core bodies at sentence boundaries, longest first, then
        # 3. core bodies -- but never below a floor that still holds a real
        # explanation. Clipping keeps the opening sentences, which is where a
        # definition lives, and drops trailing elaboration.
        for tiers, body_floor in (
            ({"supporting", "mention", "off_topic"}, 160),
            ({"core"}, 420),
        ):
            if total() <= target_chars:
                break
            candidates = sorted(
                (
                    unit
                    for unit in trimmed
                    if str(unit.get("topic_importance", "") or "") in tiers
                ),
                key=lambda unit: len(self._raw_unit_text(unit)),
                reverse=True,
            )
            for unit in candidates:
                if total() <= target_chars:
                    break
                body = str(unit.get("body", "") or "")
                if len(body) <= body_floor + 80:
                    continue
                overshoot = total() - target_chars
                unit["body"] = self._compact_summary_text(
                    body, max_chars=max(body_floor, len(body) - overshoot)
                )
        return trimmed

    def _summarized_unit_budget(self, *, context: dict[str, Any]) -> int:
        source_chars = len(str(context.get("transcript_text", "") or ""))
        return max(4, min(14, (source_chars + 5999) // 6000))

    def _compact_summarized_units(
        self,
        *,
        units: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        input_chars = sum(len(self._raw_unit_text(unit)) for unit in units)
        budget = self._summarized_unit_budget(context=context)
        off_topic_units = [
            unit
            for unit in units
            if str(unit.get("content_type", "") or "").strip().lower() == "off-topic"
        ]
        academic_units = [unit for unit in units if unit not in off_topic_units]
        selected_units = list(academic_units)
        if len(selected_units) > budget:
            selected_indexes = sorted(
                {
                    round(position * (len(selected_units) - 1) / (budget - 1))
                    for position in range(budget)
                }
            )
            selected_units = [selected_units[index] for index in selected_indexes]
        # These short, explicitly labeled side discussions are not academic
        # content, but a summary must not silently reclassify or discard them.
        # Keep at most the opening and closing discussion in their source order.
        selected_units.extend(off_topic_units[:2])
        selected_units.sort(key=lambda unit: (unit.get("start_ts", 0.0), unit.get("order_index", 0)))

        compacted: list[dict[str, Any]] = []
        kept_examples = 0
        kept_notes = 0
        for unit in selected_units:
            current = deepcopy(unit)
            current["body"] = self._compact_summary_text(current.get("body"), max_chars=700)
            current["note"] = self._compact_summary_text(current.get("note"), max_chars=320)
            current["example"] = self._compact_summary_text(current.get("example"), max_chars=650)
            raw_items = current.get("list_items", [])
            if isinstance(raw_items, list):
                current["list_items"] = [
                    self._compact_summary_text(item, max_chars=220)
                    for item in raw_items[:5]
                    if str(item or "").strip()
                ]
            else:
                current["list_items"] = []

            if current["note"]:
                if kept_notes >= 4:
                    current["note"] = ""
                else:
                    kept_notes += 1
            if current["example"]:
                if kept_examples >= 2:
                    current["example"] = ""
                else:
                    kept_examples += 1
            compacted.append(current)

        output_chars = sum(len(self._raw_unit_text(unit)) for unit in compacted)
        return compacted, {
            "enabled": True,
            "unit_budget": budget,
            "input_unit_count": len(units),
            "output_unit_count": len(compacted),
            "removed_unit_count": max(0, len(units) - len(compacted)),
            "input_content_chars": input_chars,
            "output_content_chars": output_chars,
            "content_ratio": round(output_chars / max(1, input_chars), 4),
            "kept_example_count": kept_examples,
            "kept_note_count": kept_notes,
            "kept_off_topic_count": min(2, len(off_topic_units)),
        }

    def _compact_summarized_document(
        self,
        *,
        document: dict[str, Any],
        context: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        compacted = deepcopy(document)
        pages = compacted.get("pages")
        if not isinstance(pages, list):
            return compacted, {"enabled": True, "input_block_count": 0, "output_block_count": 0}

        input_blocks = 0
        input_chars = 0
        kept_examples = 0
        kept_notes = 0
        normalized_pages: list[dict[str, Any]] = []
        for page in pages:
            if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
                continue
            normalized_blocks: list[dict[str, Any]] = []
            for raw_block in page["blocks"]:
                if not isinstance(raw_block, dict):
                    continue
                input_blocks += 1
                block = deepcopy(raw_block)
                block_type = canonical_semantic_block_type(
                    block.get("type") or block.get("block_type") or block.get("component")
                )
                block["type"] = block_type
                input_chars += self._semantic_block_content_length(block)

                # Direct-mode input can be short enough to avoid chunking while
                # still containing a full detailed note. These lower limits are
                # deliberately independent of model prompt compliance.
                field_limit = 420 if block_type in {"paragraph", "example-box"} else 260
                for field in ("content", "text", "caption"):
                    if block.get(field):
                        block[field] = self._compact_summary_text(block[field], max_chars=field_limit)
                items = block.get("items")
                if isinstance(items, list):
                    item_limit = 6 if block_type == "summary-box" else 3
                    block["items"] = [
                        self._compact_summary_text(item, max_chars=160)
                        for item in items[:item_limit]
                        if str(item or "").strip()
                    ]
                normalized_blocks.append(block)
            normalized_pages.append({**page, "blocks": normalized_blocks})

        selected_pages, selection_report = self._select_summarized_direct_blocks(
            pages=normalized_pages,
            context=context,
        )
        for page in selected_pages:
            for block in page.get("blocks", []):
                block_type = str(block.get("type", "") or "")
                if block_type == "example-box":
                    kept_examples += 1
                if block_type in {"note-box", "exam-point", "key-highlight"}:
                    kept_notes += 1

        output_blocks = sum(len(page.get("blocks", [])) for page in selected_pages)
        output_chars = sum(
            self._semantic_block_content_length(block)
            for page in selected_pages
            for block in page.get("blocks", [])
        )
        compacted["pages"] = selected_pages

        return compacted, {
            "enabled": True,
            "mode": "direct",
            "input_block_count": input_blocks,
            "output_block_count": output_blocks,
            "input_content_chars": input_chars,
            "output_content_chars": output_chars,
            "content_ratio": round(output_chars / max(1, input_chars), 4),
            "kept_example_count": kept_examples,
            "kept_note_count": kept_notes,
            "source_char_count": len(str(context.get("transcript_text", "") or "")),
            **selection_report,
        }

    @staticmethod
    def _semantic_block_content_length(block: dict[str, Any]) -> int:
        fields = ("title", "subtitle", "content", "text", "caption", "formula", "label")
        total = sum(len(str(block.get(field, "") or "")) for field in fields)
        items = block.get("items")
        if isinstance(items, list):
            total += sum(len(str(item or "")) for item in items)
        return total

    def _select_summarized_direct_blocks(
        self,
        *,
        pages: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Keep representative major-topic groups for a direct summarized note."""
        topic_groups: list[list[dict[str, Any]]] = []
        leading_blocks: list[dict[str, Any]] = []
        summary_blocks: list[dict[str, Any]] = []
        unrelated_discussion_blocks: list[dict[str, Any]] = []
        current_group: list[dict[str, Any]] = []

        for page in pages:
            for block in page.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type", "") or "")
                if block_type == "summary-box":
                    summary_blocks.append(block)
                    continue
                if block_type == "unrelated-discussion-box":
                    unrelated_discussion_blocks.append(block)
                    continue
                if block_type in {"chapter-header", "section-header"}:
                    if current_group:
                        topic_groups.append(current_group)
                    if block_type == "chapter-header" and not topic_groups and not leading_blocks:
                        leading_blocks.append(block)
                        current_group = []
                    else:
                        current_group = [block]
                    continue
                current_group.append(block)
        if current_group:
            topic_groups.append(current_group)

        source_chars = len(str(context.get("transcript_text", "") or ""))
        topic_budget = max(2, min(5, (source_chars + 1399) // 1400))
        if not topic_groups:
            topic_groups = [[]]
        selected_indexes = sorted(
            {
                round(position * (len(topic_groups) - 1) / max(1, topic_budget - 1))
                for position in range(min(topic_budget, len(topic_groups)))
            }
        )
        selected_groups = [topic_groups[index] for index in selected_indexes]

        retained: list[dict[str, Any]] = list(leading_blocks[:1])
        retained.extend(unrelated_discussion_blocks[:2])
        kept_examples = 0
        kept_callouts = 0
        for group in selected_groups:
            header_types = {"chapter-header", "section-header", "subsection-header"}
            headers = [block for block in group if str(block.get("type", "")) in header_types]
            content = [block for block in group if str(block.get("type", "")) not in header_types]
            retained.extend(headers[:2])

            ranked_content = sorted(
                enumerate(content),
                key=lambda item: (-self._summarized_block_priority(item[1]), item[0]),
            )
            chosen_indexes: set[int] = set()
            for index, block in ranked_content:
                block_type = str(block.get("type", "") or "")
                if block_type == "example-box":
                    if kept_examples >= 1:
                        continue
                    kept_examples += 1
                if block_type in {"note-box", "exam-point", "key-highlight"}:
                    if kept_callouts >= 2:
                        continue
                    kept_callouts += 1
                chosen_indexes.add(index)
                if len(chosen_indexes) >= 2:
                    break
            retained.extend(block for index, block in enumerate(content) if index in chosen_indexes)

        if summary_blocks:
            primary_summary = deepcopy(summary_blocks[0])
            merged_items: list[str] = []
            for block in summary_blocks:
                for item in block.get("items", []) if isinstance(block.get("items"), list) else []:
                    text = str(item or "").strip()
                    if text and text not in merged_items:
                        merged_items.append(text)
            primary_summary["items"] = merged_items[:6]
            retained.append(primary_summary)

        if not retained:
            retained = [
                block
                for page in pages
                for block in page.get("blocks", [])
                if isinstance(block, dict)
            ][:4]
        first_page = deepcopy(pages[0]) if pages else {"header": {}, "footer": {}}
        first_page["blocks"] = retained
        return [first_page], {
            "topic_group_count": len(topic_groups),
            "topic_budget": topic_budget,
            "selected_topic_group_count": len(selected_groups),
            "kept_unrelated_discussion_count": min(2, len(unrelated_discussion_blocks)),
            "selection_strategy": "major_topics_evenly_spaced",
        }

    @staticmethod
    def _summarized_block_priority(block: dict[str, Any]) -> int:
        block_type = str(block.get("type", "") or "")
        return {
            "formula-box": 6,
            "code-block": 6,
            "table-container": 6,
            "list": 5,
            "paragraph": 4,
            "note-box": 3,
            "exam-point": 3,
            "key-highlight": 3,
            "example-box": 2,
            "figure-reference": 1,
        }.get(block_type, 2)

    def _build_semantic_blocks_from_units(
        self,
        *,
        lecture: Lecture,
        context: dict[str, Any],
        units: list[dict[str, Any]],
        document_title: str = "",
    ) -> list[dict[str, Any]]:
        if not units:
            empty_message = (
                "No meaningful content was detected for the final note."
                if self._output_direction(context=context) == "ltr"
                else "محتوای معناداری برای ساخت جزوه نهایی تشخیص داده نشد."
            )
            return [
                {
                    "type": "paragraph",
                    "variants": ["lead"],
                    "content": empty_message,
                }
            ]

        quality_rev = quality_rev_on(context)
        # Revision 3 ("v4.2" / "v2.2"): no per-section keyword line, no highlight repeating the body,
        # none of the note's own additions, no leaked labels, a visible mark for every exam remark.
        v42 = quality_rev_v42(context)
        if quality_rev:
            supported = supported_numbers(str(context.get("transcript_text", "") or ""))
            units = [scrub_unit(unit, supported) for unit in units]
            if v42:
                units = clean_units_v42(
                    units,
                    concept_key=self._concept_key,
                    keeps_lecturer_wording=str(context.get("note_pedagogy", "") or "") != "v4",
                )
        # v4.1: topic-planned teaching notes get fuller sections, the key idea at the end of
        # its topic, numbered subsections, an intro line per section and a budget for additions.
        teaching_shape = quality_rev and any(unit.get("topic_id") for unit in units)
        if teaching_shape:
            units = budget_ai_additions(merge_thin_units(units), topic_wide=v42)
        elif quality_rev:
            # v2.1: a one-sentence fragment joins the passage it belongs to instead of
            # becoming a heading of its own.
            units = merge_thin_units(units)
        if quality_rev:
            # A split topic re-introduced itself in every part; keep each point once per section.
            units = drop_repeated_sentences(units, overlap=V42_REPEAT_OVERLAP) if v42 else drop_repeated_sentences(units)
            # One box per formula in the whole note, and no code fragments dressed as formulas.
            units = clean_unit_formulas(units, drop_trivial=v42)
        # Phase B (2026-11-18 fix plan): guards that compare the note with the transcript.
        self._term_guard_report = {}
        guards_on = bool(v42 and getattr(settings, "NOTE_QUALITY_V42_GUARDS_ENABLED", False))
        exam_cue_stems: list[set[str]] = []
        exam_guard_on = False
        if guards_on:
            guard_chunk_texts = [str(chunk.get("text") or "") for chunk in context.get("transcript_chunks") or []]
            units, self._term_guard_report = apply_term_guard(
                units,
                chunk_texts=guard_chunk_texts,
                allowed_text=" ".join(
                    [
                        str(lecture.course_name or ""),
                        str(lecture.title or ""),
                        json.dumps(context.get("slide_context") or {}, ensure_ascii=False, default=str),
                    ]
                ),
                mode=term_guard_mode(getattr(settings, "NOTE_QUALITY_V42_TERM_GUARD", "report")),
                transcript_text=str(context.get("transcript_text", "") or ""),
            )
            # With no transcript chunks (text-only input) there is nothing to check an exam mark against.
            exam_guard_on = bool(guard_chunk_texts)
            exam_cue_stems = cue_stem_sets(guard_chunk_texts)
        earlier_exam_sentences: list[str] = []
        pending_key_idea = ""
        subsection_counter = 0

        # The upload form stores the course name as the lecture title, so the main
        # title would just repeat the course. When the user gave no distinct title,
        # the planner's title for what was actually taught heads the note and the
        # course becomes its subtitle.
        user_title = str(context.get("lecture", {}).get("title") or lecture.title or "").strip()
        if v42 and looks_like_file_name(user_title):
            user_title = ""  # an upload name is not a title; the planner's title heads the note
        course_name = str(lecture.course_name or "").strip()
        ai_title = str(document_title or "").strip()
        if v42 and looks_like_file_name(ai_title):
            # A planner title that is a date, a time or «Session n»: head the note with the first topic taught.
            ai_title = next(
                (
                    str(unit.get(key) or "").strip()
                    for unit in units
                    for key in ("concept_title", "section_title")
                    if str(unit.get(key) or "").strip() and not looks_like_file_name(str(unit.get(key) or ""))
                ),
                "",
            )
        title_is_generic = not user_title or self._concept_key(user_title) == self._concept_key(course_name)
        if ai_title and title_is_generic and self._concept_key(ai_title) != self._concept_key(course_name):
            chapter_title, chapter_subtitle = ai_title, course_name
        else:
            chapter_title = user_title or ai_title
            chapter_subtitle = (
                course_name if self._concept_key(course_name) != self._concept_key(chapter_title) else ""
            )
        chapter_header: dict[str, Any] = {
            "type": "chapter-header",
            "number": "",
            "title": chapter_title,
            "subtitle": chapter_subtitle,
        }
        if any(unit.get("slide_pages") for unit in units):
            slide_name = str(context.get("slide_source_name") or "").strip()
            if slide_name:
                chapter_header["slide_name"] = slide_name
        blocks: list[dict[str, Any]] = [chapter_header]
        # Only v4 separates TesaNote's own additions from the lecturer's words,
        # and a strict-source course never carries any.
        mark_ai_added = True
        labels = semantic_labels_for_language(str(context.get("lecture", {}).get("output_language") or "fa"))
        current_section = ""
        first_body_consumed = False
        used_anchor_ids: set[str] = set()
        key_idea_scopes: set[str] = set()

        for unit in units:
            opened_section_now = False
            subsection_shown_now = False
            concept_title = str(unit.get("concept_title", "") or "").strip()
            section_title = str(unit.get("section_title", "") or "").strip() or concept_title
            content_type = str(unit.get("content_type", "explanation") or "explanation")
            # Off-topic content must never show a slide reference. Guarded
            # again here (not just in _normalize_content_unit) in case a
            # unit reaches this point from a path that skipped that step.
            is_off_topic_unit = content_type == "off-topic"
            slide_pages = (
                [] if is_off_topic_unit else list(unit.get("slide_pages", []) or [])
            )
            relevant_pages = (
                []
                if is_off_topic_unit
                else self._resolve_unit_slide_pages(context=context, unit=unit)
            )
            body = str(unit.get("body", "") or "").strip()
            note = str(unit.get("note", "") or "").strip()
            example = str(unit.get("example", "") or "").strip()
            list_items = [
                str(item).strip() for item in unit.get("list_items", []) if str(item).strip()
            ]
            exam_statements: list[tuple[str, str]] = []
            if not is_off_topic_unit:
                body, found = extract_exam_statements(body, strict=quality_rev, broad=v42)
                exam_statements.extend(found)
                note, found = extract_exam_statements(note, strict=quality_rev, broad=v42)
                exam_statements.extend(found)
                example, found = extract_exam_statements(example, strict=quality_rev, broad=v42)
                exam_statements.extend(found)
                cleaned_items: list[str] = []
                for item in list_items:
                    item, found = extract_exam_statements(item, strict=quality_rev, broad=v42)
                    exam_statements.extend(found)
                    if item:
                        cleaned_items.append(item)
                list_items = cleaned_items
                if v42:
                    body = strip_leaked_labels(body)
                    note = strip_leaked_labels(note)
                    example = strip_leaked_labels(example)
                    list_items = [item for item in (strip_leaked_labels(item) for item in list_items) if item.strip()]
            # Attach the slide reference to exactly one field per unit --
            # repeating it on body, note, example, and every list item
            # separately produced a visibly spammy wall of duplicate
            # "[[slide:N]]" chips for a single concept.
            if slide_pages:
                if body:
                    body = self._with_slide_marker(body, slide_pages)
                elif example:
                    example = self._with_slide_marker(example, slide_pages)
                elif note:
                    note = self._with_slide_marker(note, slide_pages)
                elif list_items:
                    list_items[0] = self._with_slide_marker(list_items[0], slide_pages)
            sql_query, body = self._extract_sql_query(text=body, relevant_pages=relevant_pages)

            if content_type == "off-topic":
                discussion_parts = [body, note, example, *list_items]
                discussion = " ".join(part for part in discussion_parts if part).strip()
                if discussion:
                    anchor_id = str(unit.get("topic_anchor", "") or "").strip()
                    # HTML ids must be unique; if this topic produced more
                    # than one off-topic unit, only the first box becomes
                    # the TOC's link target.
                    if anchor_id in used_anchor_ids:
                        anchor_id = ""
                    elif anchor_id:
                        used_anchor_ids.add(anchor_id)
                    if blocks and blocks[-1].get("type") == "unrelated-discussion-box":
                        # Back-to-back remarks share one box instead of
                        # repeating the same heading once per remark.
                        blocks[-1]["content"] = f"{blocks[-1]['content']} {discussion}".strip()
                        if anchor_id and not blocks[-1].get("anchor_id"):
                            blocks[-1]["anchor_id"] = anchor_id
                        continue
                    blocks.append(
                        {
                            "type": "unrelated-discussion-box",
                            "title": labels["unrelated_discussion_title"],
                            "content": discussion,
                            "anchor_id": anchor_id,
                        }
                    )
                continue

            if (
                teaching_shape
                and pending_key_idea
                and section_title
                and self._concept_key(section_title) != self._concept_key(current_section)
            ):
                blocks.append({"type": "key-highlight", "title": labels["key_idea_title"], "content": pending_key_idea})
                pending_key_idea = ""
            if section_title and self._concept_key(section_title) != self._concept_key(current_section):
                topic_anchor = str(unit.get("topic_anchor", "") or "").strip()
                topic_order = unit.get("topic_order")
                if topic_anchor in used_anchor_ids:
                    topic_anchor = ""
                elif topic_anchor:
                    used_anchor_ids.add(topic_anchor)
                blocks.append(
                    {
                        "type": "section-header",
                        "number": str(topic_order) if topic_order else "",
                        "title": section_title,
                        "anchor_id": topic_anchor,
                    }
                )
                current_section = section_title
                subsection_counter = 0
                if teaching_shape and not v42:
                    intro = section_intro_text(unit, label=labels["section_intro_label"])
                    if intro:
                        blocks.append({"type": "paragraph", "content": intro, "variants": ["section-intro"]})
                # In topic-planned notes (v2/v3/v4) the first unit of a topic is introduced
                # by the topic heading itself; a second heading right under it only
                # repeats it. Notes without topics keep their concept headings.
                opened_section_now = bool(unit.get("topic_anchor"))
            elif concept_title and self._concept_key(concept_title) != self._concept_key(section_title):
                subsection_block: dict[str, Any] = {"type": "subsection-header", "title": concept_title}
                topic_order = unit.get("topic_order")
                if teaching_shape and topic_order:
                    subsection_counter += 1
                    subsection_block["number"] = f"{topic_order}.{subsection_counter}"
                blocks.append(subsection_block)
                subsection_shown_now = True

            # A "warning" unit with no separate note text renders its body
            # inside the note-box below instead of a plain paragraph above
            # it -- rendering both duplicated the exact same sentence twice.
            body_rendered_as_note_box = content_type == "warning" and not note and bool(body)

            key_idea = str(unit.get("key_idea", "") or "").strip()
            key_idea_scope = str(unit.get("topic_anchor", "") or "") or self._concept_key(section_title)
            if key_idea and key_idea_scope not in key_idea_scopes:
                key_idea_scopes.add(key_idea_scope)
                if v42:
                    # The key idea is kept only for the closing summary; showing it after the body repeated it.
                    pass
                elif teaching_shape:
                    # Shown after the topic as the line to remember, not above it.
                    pending_key_idea = key_idea
                else:
                    blocks.append({"type": "key-highlight", "content": key_idea})

            if body and not body_rendered_as_note_box:
                # Only teaching mode keeps blank-line paragraph breaks; every
                # other body is a single whitespace-collapsed paragraph.
                body_paragraphs = [part for part in body.split("\n\n") if part.strip()]
                list_after_paragraph = -1
                if quality_rev and list_items and len(body_paragraphs) > 1:
                    # «… از جمله این موارد:» must be followed by its list, not by the next paragraph.
                    list_after_paragraph = next(
                        (
                            position
                            for position, part in enumerate(body_paragraphs[:-1])
                            if part.rstrip().endswith((":", "："))
                        ),
                        -1,
                    )
                for paragraph_index, paragraph_text in enumerate(body_paragraphs):
                    paragraph_block: dict[str, Any] = {"type": "paragraph", "content": paragraph_text}
                    if not first_body_consumed:
                        paragraph_block["variants"] = ["lead"]
                        first_body_consumed = True
                    elif content_type == "summary":
                        paragraph_block["variants"] = ["compact"]
                    blocks.append(paragraph_block)
                    if paragraph_index == list_after_paragraph:
                        blocks.append(
                            {"type": "list", "title": "", "items": list_items, "ordered": bool(unit.get("list_ordered"))}
                        )
                        list_items = []

            if mark_ai_added and not is_off_topic_unit:
                for ai_text in unit.get("ai_added", []) or []:
                    if str(ai_text).strip():
                        blocks.append(
                            {"type": "paragraph", "content": str(ai_text).strip(), "variants": ["ai-added"]}
                        )

            for formula in unit.get("formulas", []) or []:
                if str(formula).strip():
                    blocks.append({"type": "formula-box", "formula": str(formula).strip()})

            for code_block in unit.get("code_blocks", []) or []:
                for chunk, first_line in split_code_for_pages(code_block.get("code", "")):
                    block_payload: dict[str, Any] = {
                        "type": "code-block",
                        "language": code_block.get("language", "text"),
                        "code": chunk,
                    }
                    if first_line == 1 and code_block.get("caption"):
                        block_payload["caption"] = code_block["caption"]
                    if first_line > 1:
                        block_payload["start_line"] = first_line
                    blocks.append(block_payload)

            unit_tables = self._normalize_tables(unit.get("tables"))
            for table in unit_tables:
                blocks.append({"type": "table-container", **table})

            if sql_query:
                blocks.append(
                    {
                        "type": "formula-box",
                        "formula": sql_query,
                        "label": "SQL",
                        "display_mode": "query",
                    }
                )

            if list_items:
                blocks.append(
                    {
                        "type": "list",
                        "title": ""
                        if concept_title == section_title
                        or opened_section_now
                        or (quality_rev and subsection_shown_now)
                        else concept_title,
                        "items": list_items,
                        "ordered": bool(unit.get("list_ordered")),
                    }
                )

            if note:
                blocks.append({"type": "note-box", "title": labels["note_title"], "content": note})
            elif body_rendered_as_note_box:
                blocks.append({"type": "note-box", "title": labels["note_title"], "content": body})

            if example:
                example_block: dict[str, Any] = {
                    "type": "example-box",
                    "title": labels["example_title"],
                    "content": example,
                }
                if mark_ai_added and unit.get("example_ai"):
                    example_block["origin"] = "ai"
                blocks.append(example_block)

            seen_exam: set[str] = set()
            demoted_exam_sentences: list[str] = []
            if exam_guard_on and exam_statements:
                # An exam mark needs a cue of the lecturer behind it; otherwise it is a plain note.
                exam_statements, demoted_exam_sentences = demote_unbacked_exam_statements(
                    exam_statements, exam_cue_stems
                )
            if quality_rev:
                # One box per status for the unit, and nothing the note already said.
                exam_statements = merge_exam_statements(exam_statements, earlier_exam_sentences)
            for status, sentence in exam_statements:
                key = self._concept_key(sentence)
                if key in seen_exam:
                    continue
                seen_exam.add(key)
                excluded = status == "excluded"
                blocks.append(
                    {
                        "type": "exam-point",
                        "status": status,
                        "icon": "🚫" if excluded else "🎯",
                        "title": labels["exam_excluded_title" if excluded else "exam_important_title"],
                        "content": sentence,
                    }
                )

            for demoted in demoted_exam_sentences:
                if str(context.get("note_pedagogy", "") or "") == "v4":
                    blocks.append({"type": "note-box", "title": labels["note_title"], "content": demoted})
                else:
                    blocks.append({"type": "paragraph", "content": demoted})

            if content_type == "figure":
                caption = self._with_slide_marker(
                    concept_title or body or labels["figure"],
                    slide_pages,
                )
                blocks.append({"type": "figure-reference", "number": labels["figure"], "caption": caption})
            if content_type == "table" and not unit_tables:
                caption = self._with_slide_marker(
                    concept_title or body or labels["table"],
                    slide_pages,
                )
                blocks.append({"type": "figure-reference", "number": labels["table"], "caption": caption})

        if pending_key_idea:
            blocks.append({"type": "key-highlight", "title": labels["key_idea_title"], "content": pending_key_idea})
        if v42 and not teaching_shape:
            # v2.2 is the lecturer's own account. A closing summary made of the first sentence of each
            # section is a recap he never gave (it also repeated the text above it), so there is none.
            summary_block = None
        elif quality_rev:
            summary_block = self._build_full_coverage_summary_box(units=units, labels=labels)
        else:
            summary_block = self._build_summary_box_from_units(units=units, labels=labels)
        if summary_block:
            blocks.append(summary_block)

        return self._deduplicate_semantic_blocks(blocks)

    def _build_full_coverage_summary_box(
        self,
        *,
        units: list[dict[str, Any]],
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Quality revision: one takeaway per section of the whole note, not the first few units."""
        labels = labels or semantic_labels_for_language("fa")
        items = full_coverage_takeaways(units, concept_key=self._concept_key)
        if len(items) < 3:
            return None
        return {"type": "summary-box", "title": labels["summary_title"], "items": items}

    def _quality_rev_report(
        self,
        *,
        context: dict[str, Any],
        document: dict[str, Any],
        transcript_text: str,
    ) -> dict[str, Any]:
        """What the quality revision checked; empty for every note outside the trial."""
        if not quality_rev_on(context):
            return {}
        texts: list[str] = []
        for page in document.get("pages", []) or []:
            for block in page.get("blocks", []) or []:
                if not isinstance(block, dict) or block.get("type") in {"example-box", "formula-box"}:
                    continue
                for key in ("content", "text", "title"):
                    if isinstance(block.get(key), str):
                        texts.append(block[key])
                for item in block.get("items", []) if isinstance(block.get("items"), list) else []:
                    texts.append(item if isinstance(item, str) else str(item.get("text", "")) if isinstance(item, dict) else "")
        return {
            "revision": QUALITY_REV_V42 if quality_rev_v42(context) else QUALITY_REV,
            "numbers_not_in_transcript": numbers_missing_from_source("\n".join(texts), transcript_text),
        }

    def _build_summary_box_from_units(
        self,
        *,
        units: list[dict[str, Any]],
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        labels = labels or semantic_labels_for_language("fa")
        takeaways: list[str] = []
        spread_units = self._summary_source_units(units)
        max_takeaways = 8 if spread_units is not units else 5
        for unit in spread_units:
            if str(unit.get("content_type", "") or "").strip().lower() == "off-topic":
                continue
            title = str(unit.get("concept_title", "") or unit.get("section_title", "") or "").strip()
            body = str(unit.get("body", "") or "").strip()
            if title:
                takeaway = title
                if body:
                    first_sentence = re.split(r"(?<=[\.\!\؟])\s+", body, maxsplit=1)[0].strip()
                    if first_sentence and self._concept_key(first_sentence) != self._concept_key(title):
                        takeaway = f"{title}: {first_sentence}"
                takeaways.append(takeaway)
            elif body:
                takeaways.append(re.split(r"(?<=[\.\!\؟])\s+", body, maxsplit=1)[0].strip())
            if len(takeaways) >= max_takeaways:
                break
        cleaned = [item for item in takeaways if item]
        if len(cleaned) < 3:
            return None
        return {
            "type": "summary-box",
            "title": labels["summary_title"],
            "items": cleaned[:max_takeaways],
        }

    def _summary_source_units(self, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Units the summary box draws from.

        v1/v2 notes keep taking the first units in order. A v3 note is a set of
        topics, and summarising only the first few units made the box describe
        the opening of the lecture and nothing after it; there it takes the
        lead unit of each taught topic, core topics first when there are more
        topics than the box has room for, and keeps reading order.
        """
        if not any(unit.get("topic_importance") for unit in units):
            return units
        lead_units: list[dict[str, Any]] = []
        seen_topics: set[str] = set()
        for unit in units:
            topic_id = str(unit.get("topic_id", "") or "")
            importance = str(unit.get("topic_importance", "") or "")
            if (
                not topic_id
                or topic_id in seen_topics
                or importance in {"off_topic", "mention"}
                or str(unit.get("content_type", "") or "").strip().lower() == "off-topic"
            ):
                continue
            seen_topics.add(topic_id)
            lead_units.append(unit)
        limit = 8
        if len(lead_units) > limit:
            core_only = [unit for unit in lead_units if unit.get("topic_importance") == "core"]
            pool = core_only if len(core_only) >= 3 else lead_units
            if len(pool) > limit:
                step = len(pool) / limit
                pool = [pool[int(index * step)] for index in range(limit)]
            lead_units = pool
        return lead_units or units

    def _deduplicate_semantic_blocks(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen_signatures: set[str] = set()
        for block in blocks:
            signature = self._block_signature(block)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            deduped.append(block)
        return deduped

    def _block_signature(self, block: dict[str, Any]) -> str:
        block_type = str(block.get("type", "") or "")
        parts = [block_type]
        for key in ("title", "content", "caption", "number", "formula"):
            value = str(block.get(key, "") or "").strip()
            if value:
                parts.append(self._concept_key(value))
        items = block.get("items")
        if isinstance(items, list):
            parts.extend(self._concept_key(str(item)) for item in items[:6])
        return "|".join(parts)

    def _estimate_slide_reference_coverage(self, *, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        total_blocks = 0
        referenced = 0
        for block in blocks:
            if not isinstance(block, dict):
                continue
            total_blocks += 1
            text_fields = [
                str(block.get("content", "") or ""),
                str(block.get("caption", "") or ""),
            ]
            items = block.get("items")
            if isinstance(items, list):
                text_fields.extend(str(item or "") for item in items)
            if any("[[slide:" in text for text in text_fields):
                referenced += 1
        return {
            "blocks_with_slide_reference": referenced,
            "total_blocks": total_blocks,
            "coverage_ratio": round((referenced / total_blocks), 4) if total_blocks else 0.0,
        }

    def _concept_key(self, text: str) -> str:
        tokens = re.findall(r"[\w\u0600-\u06FF/+.#-]{3,}", text.lower())
        cleaned = [token.strip("-_.") for token in tokens if len(token.strip("-_.")) >= 3]
        return "-".join(cleaned[:10])

    def _keyword_set(self, text: str) -> set[str]:
        tokens = {
            token.strip("-_.")
            for token in re.findall(r"[\w\u0600-\u06FF/+.#-]{3,}", text.lower())
            if len(token.strip("-_.")) >= 3
        }
        return tokens - PERSIAN_STOPWORDS - ENGLISH_STOPWORDS

    def _split_note_sentences(self, text: str) -> list[str]:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        if not cleaned:
            return []
        rough_sentences = re.split(
            r"(?<=[\.\!\؟؛])\s+|\n+|(?<=\])\s+(?=[\u0600-\u06FFA-Za-z])",
            cleaned,
        )
        sentences: list[str] = []
        for sentence in rough_sentences:
            stripped = sentence.strip()
            if not stripped:
                continue
            if len(stripped) > 520:
                clauses = [
                    part.strip()
                    for part in re.split(r"(?<=،)\s+", stripped)
                    if part.strip()
                ]
                sentences.extend(clauses if len(clauses) > 1 else [stripped])
            else:
                sentences.append(stripped)
        return sentences

    def _extract_equivalent_terms(self, text: str) -> str:
        terms: list[str] = []
        for parenthetical in re.findall(r"\(([^)]{2,80})\)", str(text or "")):
            if re.search(r"[A-Za-z\u0600-\u06FF]", parenthetical):
                terms.append(parenthetical)
        for left, right in re.findall(
            r"([\w\u0600-\u06FF.+#-]{2,})\s*[/=]\s*([\w\u0600-\u06FF.+#-]{2,})",
            str(text or ""),
        ):
            terms.extend([left, right])
        return " ".join(terms)

    def _overlap_ratio(self, left: set[str], right: set[str]) -> float:
        if not left or not right:
            return 0.0
        return len(left & right) / max(1, min(len(left), len(right)))

    def _sentence_similarity(self, left: str, right: str) -> float:
        left_tokens = self._keyword_set(left)
        right_tokens = self._keyword_set(right)
        if not left_tokens or not right_tokens:
            return 0.0
        containment = len(left_tokens & right_tokens) / max(
            1, min(len(left_tokens), len(right_tokens))
        )
        jaccard = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
        return max(jaccard, containment * 0.9)

    def _looks_like_definition_sentence(self, sentence: str) -> bool:
        text = str(sentence or "").strip().lower()
        if not text:
            return False
        definition_patterns = [
            r"\b(is|are|means|refers to|defined as)\b",
            r"(عبارت است از|تعریف می‌شود|تعریف میشود|یعنی|به معنای|منظور از|"
            r"گفته می‌شود|گفته میشود)",
            r"(چیست|هستند|است که|حالتی است|مفهومی است|فرایندی است|روشی است|مدلی است)",
        ]
        return any(re.search(pattern, text) for pattern in definition_patterns)

    def _has_repeated_definition_signal(
        self, left: dict[str, Any], right: dict[str, Any]
    ) -> bool:
        left_definitions = list(left.get("definition_sentences", [])) or list(
            left.get("body_sentences", [])
        )[:1]
        right_definitions = list(right.get("definition_sentences", [])) or list(
            right.get("body_sentences", [])
        )[:1]
        for left_sentence in left_definitions:
            for right_sentence in right_definitions:
                if self._sentence_similarity(str(left_sentence), str(right_sentence)) >= 0.66:
                    return True
        return False

    def _sentence_has_new_value(self, sentence: str, existing_sentences: list[str]) -> bool:
        text = str(sentence or "")
        if re.search(
            r"(مثال|کاربرد|پیامد|نتیجه|نکته|هشدار|استثنا|محدودیت|فرمول|رابطه|"
            r"جدول|اسلاید|slide|example|formula|table|caveat|application)",
            text,
            re.IGNORECASE,
        ):
            return True
        existing_tokens = self._keyword_set(" ".join(existing_sentences))
        sentence_tokens = self._keyword_set(text)
        if self._looks_like_definition_sentence(text):
            return False
        return len(sentence_tokens - existing_tokens) >= 10

    def _unit_educational_text(self, unit: dict[str, Any]) -> str:
        return " ".join(
            part
            for part in [
                str(unit.get("concept_title", "") or ""),
                str(unit.get("section_title", "") or ""),
                str(unit.get("key_idea", "") or ""),
                str(unit.get("body", "") or ""),
                str(unit.get("note", "") or ""),
                str(unit.get("example", "") or ""),
                " ".join(
                    str(item) for item in unit.get("list_items", []) if str(item).strip()
                ),
            ]
            if part
        ).strip()

    def _clean_generated_text(self, text: str, *, preserve_paragraphs: bool = False) -> str:
        if preserve_paragraphs:
            paragraphs = [
                self._clean_generated_text(part)
                for part in re.split(r"\n\s*\n", str(text or ""))
            ]
            return "\n\n".join(part for part in paragraphs if part)
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        cleaned = re.sub(r"^(خب|حالا|ببینید|یعنی)\s+", "", cleaned)
        return cleaned

    def _normalize_code_blocks(self, value: Any) -> list[dict[str, str]]:
        if isinstance(value, (str, dict)):
            value = [value]
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, str):
                item = {"code": item}
            if not isinstance(item, dict):
                continue
            code = clean_code(item.get("code") or item.get("content") or "")
            if not code.strip():
                continue
            entry = {
                "language": normalize_language(item.get("language")),
                "code": code,
                "caption": " ".join(str(item.get("caption", "") or "").split())[:140],
            }
            if entry not in normalized:
                normalized.append(entry)
        return normalized[:6]

    def _normalize_tables(self, value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            value = [value]
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            headers = [
                " ".join(self._flatten_text_field(cell).split())
                for cell in (item.get("headers") or item.get("columns") or [])
            ]
            raw_rows = [row for row in (item.get("rows") or []) if isinstance(row, (list, tuple))]
            if not raw_rows:
                continue
            width = len(headers) or max(len(row) for row in raw_rows)
            if width < 2 or width > 8:
                continue
            headers = (headers + [""] * width)[:width]
            rows = []
            for row in raw_rows[:40]:
                cells = [" ".join(self._flatten_text_field(cell).split()) or "-" for cell in row]
                rows.append((cells + ["-"] * width)[:width])
            if not rows or not any(headers):
                continue
            entry = {
                "caption": " ".join(str(item.get("caption", "") or "").split())[:140],
                "headers": headers,
                "rows": rows,
            }
            if entry not in normalized:
                normalized.append(entry)
        return normalized[:4]

    def _normalize_formulas(self, value: Any) -> list[str]:
        if isinstance(value, str):
            value = [value]
        formulas: list[str] = []
        for item in value or []:
            text = re.sub(r"\s+", " ", self._flatten_text_field(item)).strip()
            text = re.sub(r"^(?:\$\$?|\\\(|\\\[)\s*|\s*(?:\$\$?|\\\)|\\\])$", "", text).strip()
            if text and len(text) <= 400 and text not in formulas:
                formulas.append(text)
        return formulas[:10]

    def _flatten_text_field(self, value: Any) -> str:
        """Coerce a unit text field into plain prose.

        The prompt asks the model for a plain string, but it sometimes
        answers a worked-example-style field (e.g. body/note/example) with a
        nested object instead (commonly {problem/setup, steps, result}).
        Passing that straight through str() prints Python's dict/list repr
        verbatim into the note, so every structured shape is flattened into
        readable text here before it ever reaches str().
        """
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, int | float | bool):
            return str(value)
        if isinstance(value, list):
            parts = [self._flatten_text_field(item) for item in value]
            parts = [part.strip() for part in parts if part and part.strip()]
            if not parts:
                return ""
            if len(parts) == 1:
                return parts[0]
            numbered = (
                f"{index + 1}. {self._ensure_sentence_end(part)}"
                for index, part in enumerate(parts)
            )
            return " ".join(numbered)
        if isinstance(value, dict):
            # Key names are schema artifacts (often English), not localized
            # content, so only the values are kept.
            parts = [self._flatten_text_field(item) for item in value.values()]
            parts = [part.strip() for part in parts if part and part.strip()]
            return " ".join(self._ensure_sentence_end(part) for part in parts)
        return str(value)

    @staticmethod
    def _ensure_sentence_end(text: str) -> str:
        if text and text[-1] not in ".!؟؛:":
            return f"{text}."
        return text

    def _is_duplicate_text(self, candidate: str, existing_texts: list[str]) -> bool:
        normalized_candidate = self._concept_key(candidate)
        if not normalized_candidate:
            return False
        candidate_tokens = self._keyword_set(candidate)
        for text in existing_texts:
            normalized_existing = self._concept_key(text)
            if normalized_candidate == normalized_existing:
                return True
            existing_tokens = self._keyword_set(text)
            if not existing_tokens:
                continue
            overlap = len(candidate_tokens & existing_tokens)
            union = max(1, len(candidate_tokens | existing_tokens))
            if overlap / union >= 0.72:
                return True
        return False

    def _merge_unique_segments(self, segments: list[str], *, separator: str = " ") -> str:
        unique_segments: list[str] = []
        for segment in segments:
            if not segment:
                continue
            if self._is_duplicate_text(segment, unique_segments):
                continue
            unique_segments.append(segment)
        return separator.join(unique_segments).strip()

    def _merge_text_fragments(self, left: str, right: str) -> str:
        if not left:
            return right
        if not right:
            return left
        if self._is_duplicate_text(right, [left]):
            return left
        return f"{left} {right}".strip()

    def _pick_stronger_content_type(self, *, existing: str, incoming: str) -> str:
        rank = {
            "definition": 5,
            "comparison": 4,
            "process": 4,
            "list": 4,
            "warning": 4,
            "example": 4,
            "figure": 3,
            "table": 3,
            "formula": 3,
            "off-topic": 2,
            "summary": 2,
            "explanation": 1,
        }
        return incoming if rank.get(incoming, 0) > rank.get(existing, 0) else existing

    def _with_slide_marker(self, text: str, slide_pages: list[int]) -> str:
        cleaned = str(text or "").strip()
        if not cleaned or not slide_pages or "[[slide:" in cleaned:
            return cleaned
        markers = "".join(f" [[slide:{page_number}]]" for page_number in slide_pages[:2] if page_number)
        return f"{cleaned}{markers}".strip()

    def _extract_sql_query(self, *, text: str, relevant_pages: list[dict[str, Any]]) -> tuple[str, str]:
        raw_text = str(text or "").strip()
        if not raw_text:
            return "", raw_text

        query_pattern = re.compile(
            r"(?is)\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|ALTER\s+TABLE|CREATE\s+TABLE)\b.*?;"
        )
        match = query_pattern.search(raw_text)
        query = match.group(0).strip() if match else ""
        cleaned_text = raw_text
        if query:
            cleaned_text = re.sub(re.escape(query), "", cleaned_text, count=1).strip(" :؛\n")
        else:
            for page in relevant_pages:
                page_text = str(page.get("text", "") or "")
                page_match = query_pattern.search(page_text)
                if page_match:
                    query = page_match.group(0).strip()
                    break

        if not query:
            return "", raw_text

        formatted_query = self._format_sql_query(query)
        cleaned_text = re.sub(r"(?:پرس[‌\s-]?وجوی\s*SQL\s*مربوطه?\s*به\s*این\s*صورت\s*است[:：]?)", "", cleaned_text, flags=re.IGNORECASE).strip(" :؛\n")
        return formatted_query, cleaned_text

    def _format_sql_query(self, query: str) -> str:
        normalized = re.sub(r"\s+", " ", query).strip()
        if sqlparse is not None:
            try:
                return sqlparse.format(
                    normalized,
                    reindent=True,
                    keyword_case="upper",
                    use_space_around_operators=True,
                ).strip()
            except Exception:
                return normalized
        return normalized

    def _persian_ratio(self, text: str) -> float:
        letters = re.findall(r"[A-Za-z\u0600-\u06FF]", text or "")
        if not letters:
            return 0.0
        persian = [char for char in letters if re.match(r"[\u0600-\u06FF]", char)]
        return len(persian) / len(letters)

    def _transcript_overlap_ratio(self, *, text: str, transcript_text: str) -> float:
        text_tokens = self._keyword_set(text)
        transcript_tokens = self._keyword_set(transcript_text)
        if not text_tokens or not transcript_tokens:
            return 0.0
        overlap = len(text_tokens & transcript_tokens)
        return overlap / max(1, len(text_tokens))

    def _extract_blocks_from_batch_payload(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        blocks = payload.get("blocks")
        if isinstance(blocks, list):
            return [block for block in blocks if isinstance(block, dict)]
        pages = payload.get("pages")
        if isinstance(pages, list):
            extracted: list[dict[str, Any]] = []
            for page in pages:
                if not isinstance(page, dict):
                    continue
                page_blocks = page.get("blocks")
                if isinstance(page_blocks, list):
                    extracted.extend(block for block in page_blocks if isinstance(block, dict))
            return extracted
        return []

    def _repair_incomplete_examples(
        self,
        *,
        lecture: Lecture,
        note: GeneratedNote,
        provider: GapGPTNoteProvider,
        document: dict[str, Any],
        context: dict[str, Any],
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
        """Repair only examples that demonstrably depend on missing local context.

        The pass is deliberately bounded because most examples are already complete and
        an additional model call should not be paid for unless the source signals that
        important board/deictic context was omitted by speech transcription.
        """
        repaired = deepcopy(document)
        candidates: list[dict[str, Any]] = []
        for page_index, page in enumerate(repaired.get("pages", []) or []):
            if not isinstance(page, dict):
                continue
            for block_index, block in enumerate(page.get("blocks", []) or []):
                if not isinstance(block, dict) or block.get("type") != "example-box":
                    continue
                content = str(block.get("content", "") or "").strip()
                evidence = self._example_evidence_window(
                    transcript_text=str(context.get("transcript_text", "") or ""),
                    example_text=content,
                )
                source_exposes_missing_visual = bool(
                    self.visual_example_pattern.search(evidence)
                    and not self.example_resolution_pattern.search(content)
                )
                if self._example_needs_reconstruction(content) or source_exposes_missing_visual:
                    candidates.append(
                        {
                            "page_index": page_index,
                            "block_index": block_index,
                            "block": block,
                            "evidence": evidence,
                        }
                    )

        report: dict[str, Any] = {
            "candidate_count": len(candidates),
            "attempted_count": 0,
            "reconstructed_count": 0,
            "completed_from_evidence_count": 0,
            "insufficient_evidence_count": 0,
            "failed_count": 0,
            "max_model_calls": 4,
        }
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        for candidate in candidates[:4]:
            block = candidate["block"]
            evidence = str(candidate.get("evidence", "") or "")
            relevant_pages = self._retrieve_relevant_slide_pages(
                context=context,
                text=" ".join([str(block.get("content", "") or ""), evidence]),
                top_k=4,
            )
            if len(evidence) < 35 and not relevant_pages:
                self._mark_example_source_limit(block=block, language_code=lecture.output_language)
                report["insufficient_evidence_count"] += 1
                continue

            report["attempted_count"] += 1
            self.update(
                note=note,
                status=NoteGenerationStatus.STRUCTURING,
                progress=66,
                stage_detail="RECONSTRUCTING_EXAMPLES",
                stage_message="Completing a source-grounded worked example.",
            )
            try:
                payload, llm_result = self._reconstruct_example_via_model(
                    lecture=lecture,
                    provider=provider,
                    current_block=block,
                    transcript_evidence=evidence,
                    relevant_slide_pages=relevant_pages,
                    event_callback=event_callback,
                )
            except Exception:
                self._mark_example_source_limit(block=block, language_code=lecture.output_language)
                report["failed_count"] += 1
                continue

            for usage_key in usage:
                usage[usage_key] += int(llm_result.usage.get(usage_key, 0) or 0)
            example_payload = payload.get("example")
            if not isinstance(example_payload, dict):
                self._mark_example_source_limit(block=block, language_code=lecture.output_language)
                report["failed_count"] += 1
                continue

            status = str(example_payload.get("reconstruction_status", "") or "").strip().lower()
            content = self._clean_generated_text(str(example_payload.get("content", "") or ""))
            solution = self._clean_generated_text(str(example_payload.get("solution", "") or ""))
            if status not in {"evidence_complete", "reconstructed", "insufficient"}:
                status = "insufficient"
            if status == "insufficient" or len(content) < 60:
                self._mark_example_source_limit(block=block, language_code=lecture.output_language)
                report["insufficient_evidence_count"] += 1
                continue

            # An allegedly exact completion must retain meaningful source vocabulary.
            # Otherwise it is presented as a reconstruction, not as professor-authored data.
            source_text = " ".join([evidence, self._slide_plain_text(pages=relevant_pages)])
            if status == "evidence_complete" and self._transcript_overlap_ratio(
                text=" ".join([content, solution]), transcript_text=source_text
            ) < 0.1:
                status = "reconstructed"

            block["content"] = content
            if solution:
                block["solution"] = solution
            else:
                block.pop("solution", None)
            block.pop("reconstruction_required", None)
            if status == "reconstructed":
                block["title"] = "Example" if lecture.output_language == "en" else "مثال"
                block["reconstruction_status"] = "reconstructed"
                report["reconstructed_count"] += 1
            else:
                block["title"] = "Example" if lecture.output_language == "en" else "مثال"
                block["reconstruction_status"] = "evidence_complete"
                report["completed_from_evidence_count"] += 1

        return repaired, report, usage

    def _example_needs_reconstruction(self, content: str) -> bool:
        cleaned = re.sub(r"\s+", " ", str(content or "")).strip()
        if not cleaned:
            return False
        has_visual_dependency = bool(self.visual_example_pattern.search(cleaned))
        has_resolution = bool(self.example_resolution_pattern.search(cleaned))
        dangling = bool(re.search(r"(?:[:،,]|(?:این|اینجا|به این صورت|as follows))\s*$", cleaned, re.IGNORECASE))
        has_example_cue = bool(self.example_cue_pattern.search(cleaned))
        return (
            dangling
            or len(cleaned) < 45
            or (has_visual_dependency and not has_resolution)
            or (has_example_cue and not has_resolution and len(cleaned) < 120)
        )

    def _example_evidence_window(self, *, transcript_text: str, example_text: str) -> str:
        sentences = self._split_note_sentences(transcript_text)
        if not sentences:
            return ""
        example_tokens = self._keyword_set(example_text)
        best_index = -1
        best_score = -1
        for index, sentence in enumerate(sentences):
            sentence_tokens = self._keyword_set(sentence)
            score = len(example_tokens & sentence_tokens) * 3
            if self.example_cue_pattern.search(sentence):
                score += 5
            if self.visual_example_pattern.search(sentence):
                score += 8
            if self.example_resolution_pattern.search(sentence):
                score += 2
            if score > best_score:
                best_index = index
                best_score = score
        if best_index < 0 or best_score <= 0:
            return ""
        start = max(0, best_index - 3)
        end = min(len(sentences), best_index + 5)
        return " ".join(sentences[start:end]).strip()[:2400]

    def _reconstruct_example_via_model(
        self,
        *,
        lecture: Lecture,
        provider: GapGPTNoteProvider,
        current_block: dict[str, Any],
        transcript_evidence: str,
        relevant_slide_pages: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], Any]:
        system_prompt = (
            "You are a forensic academic editor completing one worked example from imperfect lecture evidence. "
            f"Write in {language_label(lecture.output_language, locale='en')} ({lecture.output_language}). "
            "Use only the transcript window and matching slide OCR, formulas, or tables. Combine setup, substitutions, steps, and conclusion across the whole window. "
            "You may reconstruct omitted whiteboard steps only when they follow uniquely from supplied evidence. Never invent exact numbers, equations, premises, or final answers. "
            "When exact board values are absent but the teaching method is clear, produce a complete symbolic/generalized worked example and mark it reconstructed. "
            "If even a generalized example cannot be supported, return insufficient. Return JSON only."
        )
        user_prompt = (
            f"Lecture: {lecture.title}\nCourse: {lecture.course_name}\n"
            f"Current incomplete example:\n{json.dumps(current_block, ensure_ascii=False)}\n"
            f"Nearby transcript evidence:\n{transcript_evidence}\n"
            f"Matching slide evidence:\n{self._slide_context_snippet(pages=relevant_slide_pages)}\n"
            'Return {"example": {"content": string, "solution": string, "reconstruction_status": string}}.\n'
            "reconstruction_status must be exactly one of evidence_complete, reconstructed, insufficient.\n"
            "content must state a self-contained problem/scenario. solution must show supported reasoning and result.\n"
            "For reconstructed, use variables or explicitly supplied values and avoid claiming the professor wrote details that are not visible in the evidence."
        )
        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            return provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        return provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)

    @staticmethod
    def _mark_example_source_limit(*, block: dict[str, Any], language_code: str) -> None:
        if language_code == "en":
            block["title"] = "Example"
            notice = "The exact values or steps written on the board were not captured in the available audio or slides."
        else:
            block["title"] = "مثال"
            notice = "مقادیر یا مراحل دقیق نوشته‌شده روی تخته در صوت یا اسلایدهای موجود ثبت نشده است."
        content = str(block.get("content", "") or "").strip()
        if notice not in content:
            block["content"] = " ".join(part for part in [content, notice] if part)
        block.pop("solution", None)
        block.pop("reconstruction_required", None)
        block["reconstruction_status"] = "insufficient"

    def _repair_semantic_blocks(
        self,
        *,
        lecture: Lecture,
        note: GeneratedNote,
        model_name: str,
        provider: GapGPTNoteProvider,
        document: dict[str, Any],
        event_callback: Any | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        repaired = deepcopy(document)
        report: dict[str, Any] = {
            "supported_block_types": sorted(self.supported_block_types),
            "alias_converted_count": 0,
            "llm_repaired_count": 0,
            "local_fallback_count": 0,
            "unsupported_before": [],
            "unsupported_after": [],
            "llm_repair_used": False,
        }

        report["alias_converted_count"] = self._apply_local_block_aliases(document=repaired)
        unsupported_before = self._collect_unsupported_blocks(document=repaired)
        report["unsupported_before"] = unsupported_before
        if not unsupported_before:
            return repaired, report

        self.update(
            note=note,
            status=NoteGenerationStatus.STRUCTURING,
            progress=48,
            stage_detail="REPAIRING_UNSUPPORTED_BLOCKS",
            stage_message=f"Repairing {len(unsupported_before)} unsupported semantic block(s).",
        )

        try:
            llm_repaired_count = self._repair_unsupported_blocks_via_model(
                lecture=lecture,
                model_name=model_name,
                provider=provider,
                document=repaired,
                unsupported_blocks=unsupported_before,
                event_callback=event_callback,
            )
            report["llm_repair_used"] = llm_repaired_count > 0
            report["llm_repaired_count"] = llm_repaired_count
        except PipelineError as exc:
            report["llm_repair_error"] = exc.message

        remaining = self._collect_unsupported_blocks(document=repaired)
        if remaining:
            report["local_fallback_count"] = self._force_supported_blocks(
                document=repaired,
                unsupported_blocks=remaining,
                language_code=lecture.output_language or "fa",
            )
        report["unsupported_after"] = self._collect_unsupported_blocks(document=repaired)
        return repaired, report

    def _apply_local_block_aliases(self, *, document: dict[str, Any]) -> int:
        converted = 0
        for page in document.get("pages", []):
            if not isinstance(page, dict):
                continue
            blocks = page.get("blocks", [])
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                raw_type = str(block.get("type", "") or "").strip()
                canonical = self.block_type_aliases.get(raw_type)
                if canonical and canonical in self.supported_block_types:
                    block["type"] = canonical
                    converted += 1
        return converted

    def _collect_unsupported_blocks(self, *, document: dict[str, Any]) -> list[dict[str, Any]]:
        unsupported: list[dict[str, Any]] = []
        for page_index, page in enumerate(document.get("pages", [])):
            if not isinstance(page, dict):
                continue
            blocks = page.get("blocks", [])
            if not isinstance(blocks, list):
                continue
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                block_type = str(block.get("type", "") or "").strip()
                if not block_type or block_type not in self.supported_block_types:
                    unsupported.append(
                        {
                            "page_index": page_index,
                            "block_index": block_index,
                            "type": block_type or "<missing>",
                            "block": block,
                        }
                    )
        return unsupported

    def _repair_unsupported_blocks_via_model(
        self,
        *,
        lecture: Lecture,
        model_name: str,
        provider: GapGPTNoteProvider,
        document: dict[str, Any],
        unsupported_blocks: list[dict[str, Any]],
        event_callback: Any | None = None,
    ) -> int:
        if not unsupported_blocks:
            return 0

        output_language = lecture.output_language or "fa"
        system_prompt = (
            "You repair invalid semantic note blocks for an academic note renderer. "
            "You must replace each invalid block with exactly one valid block object. "
            "Do not redesign the content. Preserve meaning. Return JSON only."
        )
        compact_blocks = [
            {
                "index": index,
                "type": item["type"],
                "block": self._compact_block_for_repair(item["block"]),
            }
            for index, item in enumerate(unsupported_blocks)
        ]
        user_prompt = (
            f"Allowed block types: {', '.join(sorted(self.supported_block_types))}\n"
            f"Lecture title: {lecture.title}\n"
            f"Course name: {lecture.course_name}\n"
            f"Final note language: {language_label(output_language, locale='en')} ({output_language}).\n"
            "Repair each invalid block using ONLY the allowed block types.\n"
            "Keep repaired text in the final note language.\n"
            "If the block is bullet-like, use list.\n"
            "If it is a warning/remark, use note-box.\n"
            "If it is a worked scenario, use example-box.\n"
            "If it is plain prose, use paragraph.\n"
            'Return JSON with a single key "repaired_blocks", containing an array in the SAME ORDER.\n'
            f"Invalid blocks:\n{json.dumps(compact_blocks, ensure_ascii=False)}"
        )

        complete_json_signature = inspect.signature(provider.complete_json)
        if "event_callback" in complete_json_signature.parameters:
            payload, _result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                event_callback=event_callback,
            )
        else:
            payload, _result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
        repaired_blocks = payload.get("repaired_blocks")
        if not isinstance(repaired_blocks, list) or len(repaired_blocks) != len(unsupported_blocks):
            raise PipelineError(
                stage="NOTE_GENERATION",
                message=f"{model_name} returned an invalid unsupported-block repair payload.",
            )

        repaired_count = 0
        for unsupported, replacement in zip(unsupported_blocks, repaired_blocks, strict=False):
            if not isinstance(replacement, dict):
                continue
            normalized = self._normalize_repaired_block(
                replacement,
                language_code=output_language,
            )
            replacement_type = str(normalized.get("type", "") or "").strip()
            if replacement_type not in self.supported_block_types:
                continue
            page = document["pages"][unsupported["page_index"]]
            page["blocks"][unsupported["block_index"]] = normalized
            repaired_count += 1
        return repaired_count

    def _force_supported_blocks(
        self,
        *,
        document: dict[str, Any],
        unsupported_blocks: list[dict[str, Any]],
        language_code: str = "fa",
    ) -> int:
        converted = 0
        for unsupported in unsupported_blocks:
            page = document["pages"][unsupported["page_index"]]
            replacement = self._fallback_supported_block(
                unsupported["block"],
                language_code=language_code,
            )
            page["blocks"][unsupported["block_index"]] = replacement
            converted += 1
        return converted

    def _compact_block_for_repair(self, block: dict[str, Any]) -> dict[str, Any]:
        allowed_keys = ("type", "title", "subtitle", "content", "text", "items", "caption", "label", "formula", "number", "code", "language")
        compact: dict[str, Any] = {}
        for key in allowed_keys:
            if key not in block:
                continue
            value = block.get(key)
            if isinstance(value, list):
                compact[key] = [str(item)[:300] for item in value[:8]]
            elif isinstance(value, (str, int, float)):
                compact[key] = str(value)[:1200]
        return compact or {"raw": str(block)[:1200]}

    def _normalize_repaired_block(self, block: dict[str, Any], *, language_code: str = "fa") -> dict[str, Any]:
        labels = semantic_labels_for_language(language_code)
        normalized = dict(block)
        block_type = str(normalized.get("type", "") or "").strip()
        alias = self.block_type_aliases.get(block_type)
        if alias:
            normalized["type"] = alias
        block_type = str(normalized.get("type", "") or "").strip()
        if block_type == "list":
            items = normalized.get("items")
            if isinstance(items, str):
                normalized["items"] = [line.strip("•-* \t") for line in items.splitlines() if line.strip()]
            elif not isinstance(items, list):
                normalized["items"] = []
        if block_type == "paragraph" and not str(normalized.get("content", "") or "").strip():
            text = str(normalized.get("text", "") or normalized.get("title", "") or "").strip()
            normalized["content"] = text
        if block_type == "figure-reference":
            if not str(normalized.get("number", "") or "").strip():
                normalized["number"] = str(normalized.get("label", "") or "").strip()
            if not str(normalized.get("caption", "") or "").strip():
                normalized["caption"] = str(normalized.get("content", "") or normalized.get("text", "") or "").strip()
        if block_type in {"note-box", "example-box", "unrelated-discussion-box", "summary-box"} and not str(normalized.get("title", "") or "").strip():
            default_titles = {
                "note-box": labels["note_title"],
                "example-box": labels["example_title"],
                "unrelated-discussion-box": labels["unrelated_discussion_title"],
                "summary-box": labels["summary_title"],
            }
            normalized["title"] = default_titles[block_type]
        return normalized

    def _fallback_supported_block(self, block: dict[str, Any], *, language_code: str = "fa") -> dict[str, Any]:
        labels = semantic_labels_for_language(language_code)
        items = block.get("items")
        if isinstance(items, list) and items:
            return {
                "type": "list",
                "title": str(block.get("title", "") or "").strip(),
                "items": [str(item).strip() for item in items if str(item).strip()],
                "ordered": bool(block.get("ordered")),
            }
        formula = str(block.get("formula", "") or "").strip()
        if formula:
            return {
                "type": "formula-box",
                "formula": formula,
                "label": str(block.get("label", "") or "").strip(),
            }
        title = str(block.get("title", "") or "").strip()
        content_parts = [
            str(block.get("content", "") or "").strip(),
            str(block.get("text", "") or "").strip(),
            str(block.get("caption", "") or "").strip(),
            str(block.get("subtitle", "") or "").strip(),
        ]
        content = " ".join(part for part in content_parts if part).strip()
        if title and content:
            content = f"{title}: {content}"
        elif title:
            content = title
        return {
            "type": "paragraph",
            "variants": ["compact"],
            "content": content or labels["fallback_block"],
        }

    @staticmethod
    def _normalize_semantic_document(
        *,
        payload: dict[str, Any],
        lecture: Lecture,
        merged_summary: dict[str, Any],
    ) -> dict[str, Any]:
        pages = payload.get("pages")
        if not isinstance(pages, list):
            return payload
        labels = semantic_labels_for_language(lecture.output_language or "fa")

        normalized_pages: list[dict[str, Any]] = []
        for index, page in enumerate(pages, start=1):
            if not isinstance(page, dict):
                continue

            header = page.get("header", {})
            if isinstance(header, str):
                header = {
                    "course_name": lecture.course_name or header or lecture.title,
                    "course_detail": lecture.professor_name,
                    "session_number": lecture.session_number,
                    "lecture_badge": merged_summary.get("lecture_badge", ""),
                }
            elif isinstance(header, dict):
                candidate_course_name = header.get("course_name") or lecture.course_name or lecture.title
                if lecture.course_name and candidate_course_name == lecture.title:
                    candidate_course_name = lecture.course_name
                header = {
                    "course_name": candidate_course_name,
                    "course_detail": lecture.professor_name or header.get("course_detail") or "",
                    "session_number": lecture.session_number,
                    "lecture_badge": header.get("lecture_badge") or merged_summary.get("lecture_badge", ""),
                }
            else:
                header = {
                    "course_name": lecture.course_name,
                    "course_detail": lecture.professor_name,
                    "session_number": lecture.session_number,
                    "lecture_badge": merged_summary.get("lecture_badge", ""),
                }

            footer = page.get("footer", {})
            if isinstance(footer, str):
                footer = {
                    "page_number": f"{labels['page']} {index}",
                    "watermark": footer,
                }
            elif isinstance(footer, dict):
                footer = {
                    "page_number": footer.get("page_number") or f"{labels['page']} {index}",
                    "watermark": footer.get("watermark") or "",
                }
            else:
                footer = {
                    "page_number": f"{labels['page']} {index}",
                    "watermark": "",
                }

            blocks = page.get("blocks", [])
            if not isinstance(blocks, list):
                blocks = []

            normalized_blocks: list[dict[str, Any]] = []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                normalized_block = dict(block)
                block_type = canonical_semantic_block_type(
                    normalized_block.get("type")
                    or normalized_block.get("block_type")
                    or normalized_block.get("component")
                )
                if is_summary_semantic_block(normalized_block):
                    block_type = "summary-box"
                normalized_block["type"] = block_type
                block_text = str(normalized_block.get("text", "") or "").strip()

                if block_type in {"chapter-header", "section-header", "subsection-header"}:
                    if not str(normalized_block.get("title", "") or "").strip() and block_text:
                        normalized_block["title"] = block_text
                if block_type in {"paragraph", "note-box", "example-box", "key-highlight", "exam-point"}:
                    if not str(normalized_block.get("content", "") or "").strip() and block_text:
                        normalized_block["content"] = block_text
                if block_type == "list":
                    items = normalized_block.get("items")
                    if isinstance(items, str):
                        normalized_block["items"] = [line.strip("•-* \t") for line in items.splitlines() if line.strip()]
                    elif isinstance(items, list):
                        normalized_block["items"] = [
                            _normalized_text_item(item)
                            for item in items
                            if _normalized_text_item(item)
                        ]
                    else:
                        normalized_block["items"] = []
                if block_type == "summary-box":
                    items = normalized_block.get("items")
                    content = str(normalized_block.get("content", "") or "").strip()
                    if (not isinstance(items, list) or not items) and content:
                        bullet_items = [
                            line.strip("•-* \t")
                            for line in content.splitlines()
                            if line.strip().startswith(("-", "•", "*"))
                        ]
                        if bullet_items:
                            normalized_block["items"] = bullet_items
                if block_type == "note-box" and not str(normalized_block.get("title", "") or "").strip():
                    normalized_block["title"] = labels["note_title"]
                if block_type == "example-box" and not str(normalized_block.get("title", "") or "").strip():
                    normalized_block["title"] = labels["example_title"]
                if block_type == "summary-box" and not str(normalized_block.get("title", "") or "").strip():
                    normalized_block["title"] = labels["summary_title"]
                if block_type == "formula-box" and not str(normalized_block.get("formula", "") or "").strip() and block_text:
                    normalized_block["formula"] = block_text
                if block_type == "figure-reference":
                    if not str(normalized_block.get("number", "") or "").strip() and str(
                        normalized_block.get("label", "") or ""
                    ).strip():
                        normalized_block["number"] = str(normalized_block.get("label", "") or "").strip()
                    if not str(normalized_block.get("caption", "") or "").strip() and block_text:
                        normalized_block["caption"] = block_text
                if block_type in {"chapter-header", "section-header"} and "number" not in normalized_block:
                    normalized_block["number"] = ""
                normalized_blocks.append(normalized_block)

            normalized_pages.append(
                {
                    **page,
                    "header": header,
                    "footer": footer,
                    "blocks": normalized_blocks,
                }
            )

        payload["pages"] = normalized_pages
        return payload

    def _persist_html(self, *, lecture: Lecture, model_name: str, html: str, variant: str | None = None) -> str:
        filename = f"generated-note-{variant or normalize_note_variant(model_name)}.html"
        relative = temporary_artifact_path(
            user_id=str(lecture.user_id),
            lecture_id=str(lecture.id),
            category="renders/html",
            filename=filename,
        )
        stored = get_storage_service().save_content(
            relative_path=relative,
            content=html.encode("utf-8"),
            content_type="text/html; charset=utf-8",
        )
        StorageArtifactLifecycleService.register_temporary(
            user_id=str(lecture.user_id),
            lecture_id=str(lecture.id),
            storage_path=stored.path,
            size_bytes=stored.size,
            artifact_type="rendered_note_html",
        )
        return stored.path

    def _build_provider_event_callback(self, *, note: GeneratedNote, model_name: str) -> Any:
        def callback(event: dict[str, Any]) -> None:
            event_name = str(event.get("event", "") or "")
            attempt = int(event.get("attempt", 0) or 0)
            max_attempts = int(event.get("max_attempts", 0) or 0)
            provider_name = str(event.get("provider", "") or event.get("to_provider", "") or "").strip()
            active_model = str(event.get("model", "") or event.get("to_model", "") or model_name).strip()
            provider_label = f"{provider_name} / {active_model}" if provider_name else active_model
            if event_name == "provider_attempt":
                self.update(
                    note=note,
                    status=NoteGenerationStatus.STRUCTURING,
                    progress=30,
                    stage_detail="REQUESTING_MODEL",
                    stage_message=f"Waiting for {provider_label} response. Attempt {attempt} of {max_attempts}.",
                )
            elif event_name == "provider_waiting_for_slot":
                max_slots = int(event.get("max_slots", 0) or 0)
                self.update(
                    note=note,
                    status=NoteGenerationStatus.STRUCTURING,
                    progress=29,
                    stage_detail="WAITING_FOR_PROVIDER_CAPACITY",
                    stage_message=f"Waiting for an available {provider_label} slot ({max_slots} concurrent slot(s)).",
                )
            elif event_name == "provider_waiting_for_rate_budget":
                self.update(
                    note=note,
                    status=NoteGenerationStatus.STRUCTURING,
                    progress=29,
                    stage_detail="WAITING_FOR_PROVIDER_RATE_LIMIT",
                    stage_message=f"Waiting for available {provider_label} request capacity before sending the next AI call.",
                )
            elif event_name == "provider_slot_acquired":
                self.update(
                    note=note,
                    status=NoteGenerationStatus.STRUCTURING,
                    progress=30,
                    stage_detail="REQUESTING_MODEL",
                    stage_message=f"Provider slot acquired for {provider_label}. Sending request now.",
                )
            elif event_name == "provider_retry_sleep":
                retry_delay = event.get("retry_delay_seconds", 0)
                self.update(
                    note=note,
                    status=NoteGenerationStatus.STRUCTURING,
                    progress=31,
                    stage_detail="RETRYING_MODEL_REQUEST",
                    stage_message=f"{provider_label} failed temporarily. Retrying in {retry_delay} seconds.",
                )
            elif event_name == "provider_error":
                retryable = bool(event.get("retryable"))
                message = str(event.get("message", "") or "")
                if retryable:
                    self.update(
                        note=note,
                        status=NoteGenerationStatus.STRUCTURING,
                        progress=31,
                        stage_detail="MODEL_RETRY_SCHEDULED",
                        stage_message=f"{provider_label} returned a temporary error: {message}",
                    )
                else:
                    self.update(
                        note=note,
                        status=NoteGenerationStatus.STRUCTURING,
                        progress=33,
                        stage_detail="MODEL_REQUEST_FAILED",
                        stage_message=f"{provider_label} failed: {message}",
                    )
            elif event_name == "provider_fallback_triggered":
                to_provider = str(event.get("to_provider", "") or "")
                to_model = str(event.get("to_model", "") or "")
                self.update(
                    note=note,
                    status=NoteGenerationStatus.STRUCTURING,
                    progress=32,
                    stage_detail="SWITCHING_FALLBACK_PROVIDER",
                    stage_message=f"Primary provider was unavailable. Switching to {to_provider} ({to_model}).",
                )

        return callback
