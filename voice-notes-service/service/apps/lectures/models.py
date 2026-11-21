from __future__ import annotations

from django.db import models

from apps.accounts.models import get_system_user_pk
from apps.common.models import BaseModel
from apps.lectures.language_options import (
    DEFAULT_INPUT_LANGUAGE,
    DEFAULT_NOTE_DETAIL_MODE,
    DEFAULT_OUTPUT_LANGUAGE,
    LANGUAGE_CHOICES,
    NOTE_DETAIL_MODES,
)


class LectureStatus(models.TextChoices):
    UPLOADED = "UPLOADED", "Uploaded"
    PROCESSING = "PROCESSING", "Processing"
    FAILED = "FAILED", "Failed"
    COMPLETED = "COMPLETED", "Completed"


class LectureProcessingState(models.TextChoices):
    UPLOADED = "UPLOADED", "Uploaded"
    QUEUED = "QUEUED", "Queued"
    TRANSCRIBING = "TRANSCRIBING", "Transcribing"
    ANALYZING_SLIDES = "ANALYZING_SLIDES", "Analyzing Slides"
    GENERATING_NOTES = "GENERATING_NOTES", "Generating Notes"
    GENERATING_PDF = "GENERATING_PDF", "Generating PDF"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class SlideProcessingStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not Started"
    QUEUED = "QUEUED", "Queued"
    PROCESSING = "PROCESSING", "Processing"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class AudioProcessingStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not Started"
    QUEUED = "QUEUED", "Queued"
    NORMALIZING = "NORMALIZING", "Normalizing"
    CLEANING = "CLEANING", "Cleaning"
    REMOVING_SILENCE = "REMOVING_SILENCE", "Removing Silence"
    SPEED_OPTIMIZING = "SPEED_OPTIMIZING", "Speed Optimizing"
    CHUNKING = "CHUNKING", "Chunking"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class TranscriptionStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not Started"
    QUEUED = "QUEUED", "Queued"
    TRANSCRIBING = "TRANSCRIBING", "Transcribing"
    CLEANING = "CLEANING", "Cleaning"
    MERGING = "MERGING", "Merging"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"


class Lecture(BaseModel):
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="lectures",
        default=get_system_user_pk,
    )
    title = models.CharField(max_length=255, db_index=True)
    course_name = models.CharField(max_length=255, db_index=True)
    professor_name = models.CharField(max_length=255, blank=True)
    session_number = models.CharField(max_length=16, blank=True)
    input_language = models.CharField(
        max_length=8,
        choices=LANGUAGE_CHOICES,
        default=DEFAULT_INPUT_LANGUAGE,
    )
    output_language = models.CharField(
        max_length=8,
        choices=LANGUAGE_CHOICES,
        default=DEFAULT_OUTPUT_LANGUAGE,
    )
    note_detail_mode = models.CharField(
        max_length=16,
        choices=NOTE_DETAIL_MODES,
        default=DEFAULT_NOTE_DETAIL_MODE,
    )
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=LectureStatus.choices,
        default=LectureStatus.UPLOADED,
        db_index=True,
    )
    processing_state = models.CharField(
        max_length=32,
        choices=LectureProcessingState.choices,
        default=LectureProcessingState.UPLOADED,
        db_index=True,
    )
    processing_started_at = models.DateTimeField(null=True, blank=True)
    processing_completed_at = models.DateTimeField(null=True, blank=True)
    processing_error = models.TextField(blank=True)
    processing_progress = models.PositiveSmallIntegerField(default=0)
    slide_processing_status = models.CharField(
        max_length=32,
        choices=SlideProcessingStatus.choices,
        default=SlideProcessingStatus.NOT_STARTED,
        db_index=True,
    )
    slide_processing_progress = models.PositiveSmallIntegerField(default=0)
    slide_processing_stage = models.CharField(max_length=128, blank=True)
    slide_processing_error = models.TextField(blank=True)
    slide_processing_task_id = models.CharField(max_length=255, blank=True)
    slide_processing_debug = models.JSONField(default=dict, blank=True)
    slides_ready = models.BooleanField(default=False)
    audio_processing_status = models.CharField(
        max_length=32,
        choices=AudioProcessingStatus.choices,
        default=AudioProcessingStatus.NOT_STARTED,
        db_index=True,
    )
    audio_processing_progress = models.PositiveSmallIntegerField(default=0)
    optimized_audio_duration = models.FloatField(null=True, blank=True)
    silence_removed_seconds = models.FloatField(default=0)
    processing_metadata = models.JSONField(default=dict, blank=True)
    transcription_status = models.CharField(
        max_length=32,
        choices=TranscriptionStatus.choices,
        default=TranscriptionStatus.NOT_STARTED,
        db_index=True,
    )
    transcription_progress = models.PositiveSmallIntegerField(default=0)
    transcript_cost = models.FloatField(default=0)
    transcript_language = models.CharField(max_length=32, blank=True)
    transcript_duration_processed = models.FloatField(default=0)
    transcript_metadata = models.JSONField(default=dict, blank=True)
    merged_transcript = models.TextField(blank=True)
    hotwords_generated = models.JSONField(default=list, blank=True)
    hotword_source = models.CharField(max_length=64, blank=True)
    transcript_provider = models.CharField(max_length=64, blank=True)
    transcript_cost_estimation = models.JSONField(default=dict, blank=True)
    transcript_debug_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["course_name", "status"]),
            models.Index(fields=["user", "processing_state"]),
        ]

    def __str__(self) -> str:
        return self.title


class SlidePage(BaseModel):
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="slide_pages")
    page_number = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    structured_text = models.TextField(blank=True)
    images = models.JSONField(default=list, blank=True)
    tables = models.JSONField(default=list, blank=True)
    formulas = models.JSONField(default=list, blank=True)
    ocr_text = models.TextField(blank=True)
    raw_extracted_text = models.TextField(blank=True)
    thumbnail_path = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ("page_number",)
        constraints = [
            models.UniqueConstraint(fields=["lecture", "page_number"], name="unique_slide_page_per_lecture"),
        ]
        indexes = [
            models.Index(fields=["lecture", "page_number"]),
        ]

    def __str__(self) -> str:
        return f"{self.lecture_id}:{self.page_number}"
