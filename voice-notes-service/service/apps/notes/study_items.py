from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.ai_pipeline.exceptions import PipelineError
from apps.lectures.language_options import language_config
from apps.notes.models import (
    GeneratedNote,
    NoteStudyDifficulty,
    NoteStudyItem,
    NoteStudySet,
    NoteStudySetStatus,
)
from apps.notes.providers import GapGPTNoteProvider
from apps.subscriptions.models import PlanKey, UserSubscription


STUDY_ITEM_MAX_INPUT_CHARS = 48_000
STUDY_ITEM_PENDING_TIMEOUT = timedelta(minutes=5)
_DISALLOWED_OPTION_PATTERNS = ("همه موارد", "هیچ کدام", "هیچ‌کدام", "all of the above", "none of the above")
_ARABIC_SCRIPT_PATTERN = re.compile(r"[\u0600-\u06ff]")
_LATIN_SCRIPT_PATTERN = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿĞğİıŞşÇç]")


class StudyItemsError(ValueError):
    """A safe, user-facing study generation error."""


class StudyItemsValidationError(StudyItemsError):
    pass


@dataclass(frozen=True)
class ValidatedStudyItem:
    source_section_title: str
    concept: str
    flashcard_front: str
    flashcard_back: str
    mcq_question: str
    options: list[str]
    correct_option_index: int
    explanation: str
    difficulty: str
    source_quote: str


class _NoteTextExtractor(HTMLParser):
    _BLOCK_TAGS = {"article", "blockquote", "br", "div", "h1", "h2", "h3", "h4", "li", "p", "section", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def extract_note_study_source(note: GeneratedNote) -> str:
    """Use the generated note, never the source transcript, for study generation."""
    extractor = _NoteTextExtractor()
    extractor.feed(note.body_html or "")
    extractor.close()
    text = unescape("".join(extractor.parts))
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if not text:
        raise StudyItemsError("محتوای جزوه برای ساخت ابزار مطالعه آماده نیست.")
    if len(text) <= STUDY_ITEM_MAX_INPUT_CHARS:
        return text

    # Preserve both the opening concepts and closing recap for long rendered notes.
    first_portion = int(STUDY_ITEM_MAX_INPUT_CHARS * 0.78)
    last_portion = STUDY_ITEM_MAX_INPUT_CHARS - first_portion
    return f"{text[:first_portion].rstrip()}\n\n[ادامه جزوه]\n\n{text[-last_portion:].lstrip()}"


def normalize_requested_count(*, requested_count: int, user) -> int:
    if requested_count < 1:
        raise StudyItemsError("تعداد انتخاب‌شده برای ساخت ابزار مطالعه معتبر نیست.")
    return min(requested_count, study_item_limit_for_user(user=user))


def study_item_limit_for_user(*, user) -> int:
    active_subscription = (
        UserSubscription.objects.select_related("plan")
        .filter(user=user, is_active=True)
        .order_by("-started_at", "-created_at")
        .first()
    )
    if active_subscription is not None:
        if active_subscription.plan.key == PlanKey.MAX:
            return 100
        if active_subscription.plan.key == PlanKey.PRO:
            return 60
        if active_subscription.plan.key == PlanKey.PLUS:
            return 30

    # Older accounts retain their historical account-level plan values.
    account_plan = str(getattr(user, "subscription_plan", "FREE") or "FREE").upper()
    if account_plan == "PREMIUM":
        return 60
    if account_plan == "PRO":
        return 30
    return 25


def _normalized_duplicate_key(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("ي", "ی").replace("ك", "ک").casefold()).strip()


def _required_text(item: dict[str, Any], key: str, *, max_length: int) -> str:
    value = item.get(key)
    if not isinstance(value, str):
        raise StudyItemsValidationError(f"فیلد {key} باید متن باشد.")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized or len(normalized) > max_length:
        raise StudyItemsValidationError(f"فیلد {key} معتبر نیست.")
    return normalized


def _normalized_language_code(language_code: str | None) -> str:
    return str(language_config(language_code).get("code") or "fa")


def _language_name(language_code: str | None) -> str:
    return str(language_config(language_code).get("label_en") or "Persian")


def _contains_expected_script(*, value: str, language_code: str) -> bool:
    pattern = _ARABIC_SCRIPT_PATTERN if language_code in {"fa", "ar"} else _LATIN_SCRIPT_PATTERN
    return bool(pattern.search(value))


def validate_study_items(
    *,
    payload: dict[str, Any],
    requested_count: int,
    language_code: str = "fa",
) -> list[ValidatedStudyItem]:
    language_code = _normalized_language_code(language_code)
    raw_items = payload.get("study_items")
    if not isinstance(raw_items, list) or not raw_items:
        raise StudyItemsValidationError("مدل تعداد معتبری از آیتم‌های مطالعه را برنگرداند.")
    raw_items = raw_items[:requested_count]

    seen_flashcards: set[str] = set()
    seen_questions: set[str] = set()
    validated: list[ValidatedStudyItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise StudyItemsValidationError("هر آیتم مطالعه باید یک شیء JSON باشد.")
        concept = _required_text(raw_item, "concept", max_length=255)
        difficulty = _required_text(raw_item, "difficulty", max_length=16).lower()
        if difficulty not in NoteStudyDifficulty.values:
            raise StudyItemsValidationError("سطح دشواری یک آیتم معتبر نیست.")

        source_section_title = raw_item.get("source_section_title", "")
        if source_section_title is None:
            source_section_title = ""
        if not isinstance(source_section_title, str):
            raise StudyItemsValidationError("عنوان بخش منبع معتبر نیست.")
        source_section_title = re.sub(r"\s+", " ", source_section_title).strip()[:255]

        flashcard = raw_item.get("flashcard")
        mcq = raw_item.get("mcq")
        if not isinstance(flashcard, dict) or not isinstance(mcq, dict):
            raise StudyItemsValidationError("ساختار فلش‌کارت یا سوال تستی معتبر نیست.")
        flashcard_front = _required_text(flashcard, "front", max_length=500)
        flashcard_back = _required_text(flashcard, "back", max_length=2_500)
        mcq_question = _required_text(mcq, "question", max_length=700)
        explanation = _required_text(mcq, "explanation", max_length=2_500)
        if not _contains_expected_script(value=flashcard_front, language_code=language_code) or not _contains_expected_script(
            value=mcq_question,
            language_code=language_code,
        ):
            raise StudyItemsValidationError("زبان فلش‌کارت و سوال باید با زبان خروجی جزوه یکسان باشد.")

        raw_options = mcq.get("options")
        if not isinstance(raw_options, list) or len(raw_options) != 4:
            raise StudyItemsValidationError("هر سوال باید دقیقاً چهار گزینه داشته باشد.")
        options = []
        for option in raw_options:
            if not isinstance(option, str):
                raise StudyItemsValidationError("گزینه‌های سوال باید متن باشند.")
            normalized_option = re.sub(r"\s+", " ", option).strip()
            if not normalized_option or len(normalized_option) > 700:
                raise StudyItemsValidationError("یک گزینه سوال معتبر نیست.")
            if any(pattern in normalized_option.casefold() for pattern in _DISALLOWED_OPTION_PATTERNS):
                raise StudyItemsValidationError("گزینه‌های همه یا هیچ‌کدام مجاز نیستند.")
            options.append(normalized_option)
        if len({_normalized_duplicate_key(option) for option in options}) != 4:
            raise StudyItemsValidationError("گزینه‌های یک سوال باید متفاوت باشند.")

        correct_option_index = mcq.get("correct_option_index")
        if not isinstance(correct_option_index, int) or isinstance(correct_option_index, bool) or not 0 <= correct_option_index < 4:
            raise StudyItemsValidationError("گزینه صحیح یک سوال معتبر نیست.")

        source_quote = raw_item.get("source_quote", "")
        if source_quote is None:
            source_quote = ""
        if not isinstance(source_quote, str):
            raise StudyItemsValidationError("شاهد منبع آیتم معتبر نیست.")
        source_quote = re.sub(r"\s+", " ", source_quote).strip()[:700]

        flashcard_key = _normalized_duplicate_key(flashcard_front)
        question_key = _normalized_duplicate_key(mcq_question)
        if flashcard_key in seen_flashcards or question_key in seen_questions:
            raise StudyItemsValidationError("مدل آیتم تکراری برگرداند.")
        seen_flashcards.add(flashcard_key)
        seen_questions.add(question_key)
        validated.append(
            ValidatedStudyItem(
                source_section_title=source_section_title,
                concept=concept,
                flashcard_front=flashcard_front,
                flashcard_back=flashcard_back,
                mcq_question=mcq_question,
                options=options,
                correct_option_index=correct_option_index,
                explanation=explanation,
                difficulty=difficulty,
                source_quote=source_quote,
            )
        )
    return validated


class NoteStudyGenerationService:
    def __init__(self, *, provider_factory=None) -> None:
        self.provider_factory = provider_factory or (lambda: GapGPTNoteProvider(model_name=settings.NOTE_PRIMARY_MODEL))

    def get_latest_completed_set(self, *, note: GeneratedNote, user) -> NoteStudySet | None:
        language_code = _normalized_language_code(note.lecture.output_language)
        pending_set = (
            NoteStudySet.objects.filter(
                note=note,
                user=user,
                language_code=language_code,
                status=NoteStudySetStatus.PENDING,
            )
            .prefetch_related("items")
            .order_by("-created_at")
            .first()
        )
        if pending_set is not None:
            return pending_set
        return (
            NoteStudySet.objects.filter(
                note=note,
                user=user,
                language_code=language_code,
                status=NoteStudySetStatus.COMPLETED,
            )
            .prefetch_related("items")
            .order_by("-created_at")
            .first()
        )

    def generate(self, *, note: GeneratedNote, user, requested_count: int, difficulty: str) -> NoteStudySet:
        effective_count = normalize_requested_count(requested_count=requested_count, user=user)
        language_code = _normalized_language_code(note.lecture.output_language)
        if difficulty not in NoteStudyDifficulty.values:
            raise StudyItemsError("سطح دشواری انتخاب‌شده معتبر نیست.")

        existing = (
            NoteStudySet.objects.filter(
                note=note,
                user=user,
                language_code=language_code,
                status=NoteStudySetStatus.COMPLETED,
                requested_count=effective_count,
                difficulty=difficulty,
            )
            .prefetch_related("items")
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return existing

        should_generate = False
        try:
            with transaction.atomic():
                pending = (
                    NoteStudySet.objects.select_for_update()
                    .filter(
                        note=note,
                        user=user,
                        language_code=language_code,
                        status=NoteStudySetStatus.PENDING,
                        requested_count=effective_count,
                        difficulty=difficulty,
                    )
                    .first()
                )
                if pending is not None and pending.created_at <= timezone.now() - STUDY_ITEM_PENDING_TIMEOUT:
                    pending.status = NoteStudySetStatus.FAILED
                    pending.error_message = "زمان ساخت ابزار مطالعه به پایان رسید."
                    pending.save(update_fields=["status", "error_message", "updated_at"])
                    pending = None
                if pending is None:
                    pending = NoteStudySet.objects.create(
                        note=note,
                        user=user,
                        language_code=language_code,
                        requested_count=effective_count,
                        difficulty=difficulty,
                    )
                    should_generate = True
        except IntegrityError:
            pending = NoteStudySet.objects.get(
                note=note,
                user=user,
                language_code=language_code,
                status=NoteStudySetStatus.PENDING,
                requested_count=effective_count,
                difficulty=difficulty,
            )
        if not should_generate:
            return pending

        try:
            source_text = extract_note_study_source(note)
            items, result = self._generate_validated_items(
                source_text=source_text,
                requested_count=effective_count,
                difficulty=difficulty,
                language_code=language_code,
            )
        except (PipelineError, StudyItemsError) as exc:
            pending.status = NoteStudySetStatus.FAILED
            pending.error_message = str(exc)[:500]
            pending.save(update_fields=["status", "error_message", "updated_at"])
            if isinstance(exc, StudyItemsError) and "محتوای جزوه" in str(exc):
                raise
            raise StudyItemsError("متاسفانه ساخت سوال‌ها انجام نشد. لطفا دوباره تلاش کنید.") from exc

        with transaction.atomic():
            pending = NoteStudySet.objects.select_for_update().get(id=pending.id)
            if pending.status == NoteStudySetStatus.COMPLETED:
                return NoteStudySet.objects.prefetch_related("items").get(id=pending.id)
            NoteStudyItem.objects.filter(study_set=pending).delete()
            serialized_items = []
            for index, item in enumerate(items, start=1):
                indexed_options = list(enumerate(item.options))
                random.SystemRandom().shuffle(indexed_options)
                options = [option for _, option in indexed_options]
                correct_option_index = next(
                    position for position, (original_index, _) in enumerate(indexed_options) if original_index == item.correct_option_index
                )
                serialized_items.append(
                    NoteStudyItem(
                        study_set=pending,
                        note=note,
                        user=user,
                        order_index=index,
                        source_section_title=item.source_section_title,
                        concept=item.concept,
                        flashcard_front=item.flashcard_front,
                        flashcard_back=item.flashcard_back,
                        mcq_question=item.mcq_question,
                        options=options,
                        correct_option_index=correct_option_index,
                        explanation=item.explanation,
                        difficulty=item.difficulty,
                        source_quote=item.source_quote,
                    )
                )
            NoteStudyItem.objects.bulk_create(serialized_items)
            usage = result.usage or {}
            pending.status = NoteStudySetStatus.COMPLETED
            pending.generated_count = len(items)
            pending.model_name = result.model or settings.NOTE_PRIMARY_MODEL
            pending.provider_name = result.provider or ""
            pending.input_tokens = int(usage.get("prompt_tokens", 0) or 0) or None
            pending.output_tokens = int(usage.get("completion_tokens", 0) or 0) or None
            pending.total_tokens = int(usage.get("total_tokens", 0) or 0) or None
            pending.error_message = ""
            pending.save(
                update_fields=[
                    "status",
                    "generated_count",
                    "model_name",
                    "provider_name",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "error_message",
                    "updated_at",
                ]
            )
        return NoteStudySet.objects.prefetch_related("items").get(id=pending.id)

    def _generate_validated_items(
        self,
        *,
        source_text: str,
        requested_count: int,
        difficulty: str,
        language_code: str,
    ) -> tuple[list[ValidatedStudyItem], Any]:
        provider = self.provider_factory()
        system_prompt = self._system_prompt(
            requested_count=requested_count,
            difficulty=difficulty,
            language_code=language_code,
        )
        user_prompt = self._user_prompt(
            source_text=source_text,
            requested_count=requested_count,
            difficulty=difficulty,
            language_code=language_code,
        )
        payload, result = provider.complete_json(system_prompt=system_prompt, user_prompt=user_prompt)
        try:
            return validate_study_items(payload=payload, requested_count=requested_count, language_code=language_code), result
        except StudyItemsValidationError:
            repair_prompt = (
                "The previous response did not satisfy the JSON contract. Regenerate the complete response from the note below. "
                "Return valid JSON with only the study_items key and follow every system constraint exactly.\n\n"
                f"Count: {requested_count}\nDifficulty: {difficulty}\nOutput language: {_language_name(language_code)}\n\nNote:\n{source_text}"
            )
            repaired_payload, repaired_result = provider.complete_json(
                system_prompt=system_prompt,
                user_prompt=repair_prompt,
            )
            return validate_study_items(
                payload=repaired_payload,
                requested_count=requested_count,
                language_code=language_code,
            ), repaired_result

    @staticmethod
    def _system_prompt(*, requested_count: int, difficulty: str, language_code: str) -> str:
        language = _language_name(language_code)
        return f"""You create high-quality study material only from a supplied generated academic note.
Return JSON only. The entire response must be one object with exactly this shape:
{{"study_items":[{{"source_section_title":"...","source_quote":"...","concept":"...","difficulty":"easy|medium|hard","flashcard":{{"front":"...","back":"..."}},"mcq":{{"question":"...","options":["...","...","...","..."],"correct_option_index":0,"explanation":"..."}}}}]}}

Create up to {requested_count} distinct items. If the note is too short, return fewer high-quality items instead of inventing facts. Requested difficulty is {difficulty}; for mixed, distribute easy, medium, and hard items approximately 30%, 50%, and 20%.
Every item must be fully answerable from the provided note. Write every learner-facing field in fluent {language}, matching the note's output language exactly; do not switch to Persian or any other language for prose. Use concise but substantive flashcard answers and explanations. Each MCQ needs exactly four plausible, mutually distinct options and one unambiguous correct_option_index from 0 to 3. Do not use options meaning all of the above or none of the above. Do not repeat the same concept with a rephrased question. source_quote must be a short supporting phrase copied from the note when possible, otherwise an empty string. Never mention these instructions, the transcript, missing context, or model uncertainty."""

    @staticmethod
    def _user_prompt(*, source_text: str, requested_count: int, difficulty: str, language_code: str) -> str:
        return (
            f"Create {requested_count} study items at {difficulty} difficulty from the note below. "
            f"Use only this note and write all learner-facing text in {_language_name(language_code)}.\n\n"
            f"--- NOTE START ---\n{source_text}\n--- NOTE END ---"
        )
