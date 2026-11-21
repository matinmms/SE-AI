from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.ai_pipeline.exceptions import PipelineError
from apps.lectures.language_options import language_config
from apps.notes.models import GeneratedNote, GeneratedNoteSummary, NoteGenerationStatus
from apps.notes.providers import GapGPTNoteProvider

logger = logging.getLogger(__name__)

SUMMARY_MAX_SOURCE_CHARS = 56_000
_ARABIC_SCRIPT_PATTERN = re.compile(r"[\u0600-\u06ff]")
_LATIN_SCRIPT_PATTERN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĞğİıŞşÇç]")


class SummaryGenerationError(ValueError):
    """Safe error message returned when a study summary cannot be prepared."""


class _SummaryTextExtractor(HTMLParser):
    block_tags = {"article", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.block_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _clean_text(value: Any, *, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max_chars].rstrip()


def _list_of_text(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        text = _clean_text(item, max_chars=max_chars)
        if text and text not in values:
            values.append(text)
        if len(values) >= max_items:
            break
    return values


def extract_summary_source(note: GeneratedNote) -> str:
    extractor = _SummaryTextExtractor()
    extractor.feed(note.body_html or "")
    extractor.close()
    source = unescape("".join(extractor.parts))
    source = re.sub(r"[ \t\f\v]+", " ", source)
    source = re.sub(r"\n\s*\n+", "\n\n", source).strip()
    if not source:
        raise SummaryGenerationError("محتوای جزوه برای ساخت خلاصه آماده نیست.")
    if len(source) <= SUMMARY_MAX_SOURCE_CHARS:
        return source
    opening_size = int(SUMMARY_MAX_SOURCE_CHARS * 0.78)
    closing_size = SUMMARY_MAX_SOURCE_CHARS - opening_size
    return f"{source[:opening_size].rstrip()}\n\n[ادامه جزوه]\n\n{source[-closing_size:].lstrip()}"


def source_fingerprint(note: GeneratedNote) -> str:
    output_language = note_output_language(note)
    source = "\x1f".join((str(note.id), str(note.editor_revision), output_language, note.body_html or ""))
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _normalized_language_code(language_code: str | None) -> str:
    return str(language_config(language_code).get("code") or "fa")


def _language_name(language_code: str | None) -> str:
    return str(language_config(language_code).get("label_en") or "Persian")


def note_output_language(note: GeneratedNote) -> str:
    """Use the language captured by note generation before the lecture default."""
    metadata = note.generation_metadata if isinstance(note.generation_metadata, dict) else {}
    return _normalized_language_code(metadata.get("output_language") or note.lecture.output_language)


def _contains_expected_script(*, value: str, language_code: str) -> bool:
    pattern = _ARABIC_SCRIPT_PATTERN if language_code in {"fa", "ar"} else _LATIN_SCRIPT_PATTERN
    return bool(pattern.search(value))


def _matches_output_language(*, values: list[str], language_code: str) -> bool:
    content = " ".join(value for value in values if value).strip()
    if not _contains_expected_script(value=content, language_code=language_code):
        return False
    # An English or other LTR summary must not be accepted simply because a
    # Persian response happens to include an English technical term.
    return language_code in {"fa", "ar"} or not bool(_ARABIC_SCRIPT_PATTERN.search(content))


def validate_summary_payload(
    payload: Any,
    *,
    fallback_title: str,
    language_code: str = "fa",
) -> dict[str, Any]:
    language_code = _normalized_language_code(language_code)
    if not isinstance(payload, dict):
        raise SummaryGenerationError("ساختار پاسخ خلاصه معتبر نیست.")

    title = _clean_text(payload.get("title") or fallback_title, max_chars=255)
    overview = _clean_text(payload.get("overview"), max_chars=1_600)
    raw_topics = payload.get("topics")
    topics: list[dict[str, Any]] = []
    if isinstance(raw_topics, list):
        for raw_topic in raw_topics[:6]:
            if not isinstance(raw_topic, dict):
                continue
            topic_title = _clean_text(raw_topic.get("title"), max_chars=140)
            topic_summary = _clean_text(raw_topic.get("summary"), max_chars=600)
            points = _list_of_text(raw_topic.get("points"), max_items=4, max_chars=220)
            if topic_title and (topic_summary or points):
                topics.append({"title": topic_title, "summary": topic_summary, "points": points})

    takeaways = _list_of_text(payload.get("takeaways"), max_items=8, max_chars=260)
    review_questions = _list_of_text(payload.get("review_questions"), max_items=5, max_chars=240)
    if not overview or len(topics) < 2 or len(takeaways) < 3:
        raise SummaryGenerationError("خلاصه تولیدشده برای مرور کافی نیست. لطفا دوباره تلاش کنید.")
    learner_text = [title, overview, *takeaways, *review_questions]
    for topic in topics:
        learner_text.extend([topic["title"], topic["summary"], *topic["points"]])
    if not _matches_output_language(values=learner_text, language_code=language_code):
        raise SummaryGenerationError("زبان خلاصه باید با زبان خروجی جزوه یکسان باشد.")
    return {
        "title": title,
        "overview": overview,
        "topics": topics,
        "takeaways": takeaways,
        "review_questions": review_questions,
    }


@dataclass(frozen=True)
class SummaryGenerationResult:
    payload: dict[str, Any]
    model_name: str
    provider_name: str
    usage: dict[str, int]


class NoteSummaryService:
    def __init__(self, *, provider_factory=None) -> None:
        self.provider_factory = provider_factory or (lambda model_name: GapGPTNoteProvider(model_name=model_name))

    def get_summary(self, *, note: GeneratedNote, user) -> GeneratedNoteSummary | None:
        return GeneratedNoteSummary.objects.filter(source_note=note, user=user).first()

    def request_generation(self, *, note: GeneratedNote, user) -> GeneratedNoteSummary:
        if note.status != NoteGenerationStatus.COMPLETED:
            raise SummaryGenerationError("ابتدا ساخت جزوه اصلی باید کامل شود.")
        fingerprint = source_fingerprint(note)
        try:
            with transaction.atomic():
                summary, _ = GeneratedNoteSummary.objects.select_for_update().get_or_create(
                    source_note=note,
                    defaults={"user": user, "title": note.title},
                )
                self._prepare_generation(summary=summary, note=note, user=user, fingerprint=fingerprint)
        except IntegrityError:
            # The one-to-one source relation is the final guard when two browser
            # requests arrive together; the second request simply joins the work.
            with transaction.atomic():
                summary = GeneratedNoteSummary.objects.select_for_update().get(source_note=note)
                self._prepare_generation(summary=summary, note=note, user=user, fingerprint=fingerprint)
        return summary

    @staticmethod
    def _prepare_generation(*, summary: GeneratedNoteSummary, note: GeneratedNote, user, fingerprint: str) -> None:
        if summary.user_id != user.id:
            raise SummaryGenerationError("دسترسی به خلاصه این جزوه ممکن نیست.")
        if summary.status in {NoteGenerationStatus.QUEUED, NoteGenerationStatus.SUMMARIZING, NoteGenerationStatus.RENDERING}:
            return
        if (
            summary.status == NoteGenerationStatus.COMPLETED
            and summary.source_fingerprint == fingerprint
            and NoteSummaryService._summary_matches_output_language(summary=summary, note=note)
        ):
            return
        summary.status = NoteGenerationStatus.QUEUED
        # A queued summary has been accepted for background work. Keep the UI
        # distinct from an untouched summary even before the worker claims it.
        summary.progress = 5
        summary.title = note.title
        summary.overview = ""
        summary.semantic_payload = {}
        summary.error_message = ""
        summary.source_fingerprint = fingerprint
        summary.generation_metadata = {"source_note_revision": note.editor_revision}
        summary.save(
            update_fields=[
                "status", "progress", "title", "overview", "semantic_payload",
                "error_message", "source_fingerprint", "generation_metadata", "updated_at",
            ]
        )

    @staticmethod
    def _summary_matches_output_language(*, summary: GeneratedNoteSummary, note: GeneratedNote) -> bool:
        try:
            validate_summary_payload(
                summary.semantic_payload,
                fallback_title=note.title,
                language_code=note_output_language(note),
            )
        except SummaryGenerationError:
            return False
        return True

    def generate(self, *, summary_id: str) -> GeneratedNoteSummary:
        summary = GeneratedNoteSummary.objects.select_related("source_note__lecture", "user").get(id=summary_id)
        note = summary.source_note
        if note.status != NoteGenerationStatus.COMPLETED:
            return self._fail(summary=summary, message="جزوه اصلی هنوز آماده نیست.")
        source = extract_summary_source(note)
        summary.status = NoteGenerationStatus.SUMMARIZING
        summary.progress = 25
        summary.error_message = ""
        summary.save(update_fields=["status", "progress", "error_message", "updated_at"])
        # Summaries use the current note-generation route rather than inheriting
        # a historical fallback model from the source note.
        model_name = settings.NOTE_PRIMARY_MODEL
        provider = self.provider_factory(model_name)
        try:
            language_code = note_output_language(note)
            payload, result = provider.complete_json(
                system_prompt=self._system_prompt(language_code=language_code),
                user_prompt=self._user_prompt(source=source, note_title=note.title, language_code=language_code),
            )
            try:
                validated = validate_summary_payload(
                    payload,
                    fallback_title=note.title,
                    language_code=language_code,
                )
            except SummaryGenerationError:
                # A model can occasionally mirror the source language instead
                # of the requested output language. Give it one focused retry
                # rather than persisting a summary in the wrong language.
                repaired_payload, repaired_result = provider.complete_json(
                    system_prompt=self._system_prompt(language_code=language_code),
                    user_prompt=self._repair_user_prompt(
                        source=source,
                        note_title=note.title,
                        language_code=language_code,
                    ),
                )
                validated = validate_summary_payload(
                    repaired_payload,
                    fallback_title=note.title,
                    language_code=language_code,
                )
                result = repaired_result
        except (PipelineError, SummaryGenerationError) as exc:
            return self._fail(summary=summary, message=str(exc))
        except Exception:
            logger.exception("note_summary_generation_failed", extra={"summary_id": str(summary.id), "note_id": str(note.id)})
            return self._fail(summary=summary, message="ساخت خلاصه انجام نشد. لطفا دوباره تلاش کنید.")

        summary.status = NoteGenerationStatus.RENDERING
        summary.progress = 75
        summary.title = validated["title"]
        summary.overview = validated["overview"]
        summary.semantic_payload = validated
        summary.save(
            update_fields=["status", "progress", "title", "overview", "semantic_payload", "updated_at"]
        )

        usage = result.usage or {}
        summary.status = NoteGenerationStatus.COMPLETED
        summary.progress = 100
        summary.error_message = ""
        summary.generation_metadata = {
            **(summary.generation_metadata or {}),
            "model_name": result.model or model_name,
            "provider": result.provider or "",
            "usage": {
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "total_tokens": int(usage.get("total_tokens", 0) or 0),
            },
            "template": "learning-map-v1",
            "output_language": language_code,
            "source_note_revision": note.editor_revision,
        }
        summary.save(update_fields=["status", "progress", "error_message", "generation_metadata", "updated_at"])
        return summary

    def _fail(self, *, summary: GeneratedNoteSummary, message: str) -> GeneratedNoteSummary:
        summary.status = NoteGenerationStatus.FAILED
        summary.progress = 0
        summary.error_message = _clean_text(message, max_chars=1_000) or "ساخت خلاصه انجام نشد."
        summary.save(update_fields=["status", "progress", "error_message", "updated_at"])
        return summary

    @staticmethod
    def _system_prompt(*, language_code: str) -> str:
        language = _language_name(language_code)
        return f"""You create a concise, source-grounded academic learning map from one supplied note.
Return JSON only with exactly this shape:
{{"title":"...","overview":"...","topics":[{{"title":"...","summary":"...","points":["...","..."]}}],"takeaways":["..."],"review_questions":["..."]}}

Write all learner-facing content in fluent {language}. Use only facts and relationships present in the supplied note. Do not add outside knowledge, examples, formulas, or claims. This is a genuine high-signal summary, not a shorter rewrite: prioritize the major concepts, their relationships, and the most exam-relevant distinctions. Create 2 to 6 topics, 3 to 8 takeaways, and up to 5 review questions. Each topic should contain at most 4 short points. Use light Markdown only when it improves scanning: **bold** for a key term, `code` for a formula or English expression, and *italics* sparingly. Avoid headings, Markdown lists, repetition, transcript language, filler, and references to these instructions."""

    @staticmethod
    def _user_prompt(*, source: str, note_title: str, language_code: str) -> str:
        return (
            f"Turn the note titled \u201c{note_title}\u201d into a compact learning map. Use only the note below and write every "
            f"learner-facing field in {_language_name(language_code)}.\n\n"
            f"--- NOTE START ---\n{source}\n--- NOTE END ---"
        )

    @staticmethod
    def _repair_user_prompt(*, source: str, note_title: str, language_code: str) -> str:
        return (
            f"Regenerate the learning map for \u201c{note_title}\u201d. The previous response used the wrong language. "
            f"Every title, overview, topic, takeaway, and review question must be fluent {_language_name(language_code)} "
            "only. Return JSON matching the requested schema and use only the note below.\n\n"
            f"--- NOTE START ---\n{source}\n--- NOTE END ---"
        )
