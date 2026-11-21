from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import DatabaseError, OperationalError
from django.utils import timezone

from apps.ai_pipeline.exceptions import PipelineError
from apps.audio_pipeline.models import TranscriptChunk
from apps.lectures.models import (
    AudioProcessingStatus,
    Lecture,
    LectureStatus,
    SlideProcessingStatus,
    TranscriptionStatus,
)
from apps.notes.models import NoteGenerationStatus
from services.errors.recorder import ErrorRecordingService
from services.lectures.processing import LectureProcessingStateService
from services.usage.lifecycle import UsageLogService

ACTIVE_AUDIO_STATUSES = {
    AudioProcessingStatus.QUEUED,
    AudioProcessingStatus.NORMALIZING,
    AudioProcessingStatus.CLEANING,
    AudioProcessingStatus.REMOVING_SILENCE,
    AudioProcessingStatus.SPEED_OPTIMIZING,
    AudioProcessingStatus.CHUNKING,
}
ACTIVE_SLIDE_STATUSES = {
    SlideProcessingStatus.QUEUED,
    SlideProcessingStatus.PROCESSING,
}
ACTIVE_TRANSCRIPTION_STATUSES = {
    TranscriptionStatus.QUEUED,
    TranscriptionStatus.TRANSCRIBING,
    TranscriptionStatus.CLEANING,
    TranscriptionStatus.MERGING,
}
ACTIVE_NOTE_STATUSES = {
    NoteGenerationStatus.QUEUED,
    NoteGenerationStatus.SUMMARIZING,
    NoteGenerationStatus.STRUCTURING,
    NoteGenerationStatus.RENDERING,
}

logger = logging.getLogger("tesanote")


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        dt_value = value
    elif isinstance(value, (int, float)):
        try:
            dt_value = datetime.fromtimestamp(float(value), tz=dt_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        try:
            dt_value = datetime.fromisoformat(value)
        except ValueError:
            try:
                dt_value = datetime.fromtimestamp(float(value), tz=dt_timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    else:
        return None

    if dt_value.tzinfo is None:
        return dt_value.replace(tzinfo=dt_timezone.utc)
    return dt_value.astimezone(dt_timezone.utc)


def _latest_transcription_activity_at(lecture: Lecture) -> datetime | None:
    candidates: list[datetime] = []
    transcript_metadata = lecture.transcript_metadata or {}
    processing_metadata = lecture.processing_metadata or {}

    for key in ("last_progress_activity_at", "provider_last_activity_at", "started_at"):
        dt_value = _parse_datetime(transcript_metadata.get(key))
        if dt_value is not None:
            candidates.append(dt_value)

    for entry in transcript_metadata.get("stage_trace", []):
        if not isinstance(entry, dict):
            continue
        dt_value = _parse_datetime(entry.get("timestamp"))
        if dt_value is not None:
            candidates.append(dt_value)

    for chunk_metadata in TranscriptChunk.objects.filter(lecture=lecture).values_list("processing_metadata", flat=True):
        if not isinstance(chunk_metadata, dict):
            continue
        for key in ("submitted_at", "completed_at"):
            dt_value = _parse_datetime(chunk_metadata.get(key))
            if dt_value is not None:
                candidates.append(dt_value)

    pipeline_started_at = _parse_datetime(processing_metadata.get("pipeline_started_at"))
    if pipeline_started_at is not None:
        candidates.append(pipeline_started_at)

    return max(candidates) if candidates else None


def transcription_inactivity_seconds(*, lecture: Lecture) -> float | None:
    latest_activity_at = _latest_transcription_activity_at(lecture)
    if latest_activity_at is None:
        return None
    return (timezone.now() - latest_activity_at).total_seconds()


def _latest_note_activity_at(lecture: Lecture) -> datetime | None:
    candidates: list[datetime] = []
    processing_metadata = lecture.processing_metadata or {}

    for key in ("note_generation_last_activity_at", "note_generation_started_at", "pipeline_started_at"):
        dt_value = _parse_datetime(processing_metadata.get(key))
        if dt_value is not None:
            candidates.append(dt_value)

    for note in lecture.generated_notes.all():
        metadata = note.generation_metadata or {}
        for key in ("last_activity_at",):
            dt_value = _parse_datetime(metadata.get(key))
            if dt_value is not None:
                candidates.append(dt_value)
        for entry in metadata.get("stage_trace", []):
            if not isinstance(entry, dict):
                continue
            dt_value = _parse_datetime(entry.get("timestamp"))
            if dt_value is not None:
                candidates.append(dt_value)

    return max(candidates) if candidates else None


def _mark_processing_failure(*, lecture: Lecture, error: PipelineError, reason_code: str) -> bool:
    try:
        ErrorRecordingService.record_processing_error(
            user=lecture.user,
            lecture=lecture,
            stage=error.stage,
            error_type=error.__class__.__name__,
            error_message=error.message,
            metadata={"reason_code": reason_code},
        )
    except (OperationalError, DatabaseError) as exc:  # pragma: no cover - depends on local DB backend
        logger.warning(
            "processing_error_record_skipped",
            extra={
                "lecture_id": str(lecture.id),
                "reason_code": reason_code,
                "error": str(exc),
            },
        )
    timed_out_at = timezone.now().isoformat()
    LectureProcessingStateService.mark_failed(lecture=lecture, error_message=error.message)
    lecture.processing_progress = min(100, max(int(lecture.processing_progress or 0), 100))
    lecture.processing_metadata = {
        **(lecture.processing_metadata or {}),
        "pipeline_error": error.message,
        "pipeline_timeout_reason": reason_code,
        "pipeline_timed_out_at": timed_out_at,
    }

    if lecture.transcription_status in ACTIVE_TRANSCRIPTION_STATUSES:
        lecture.transcription_status = TranscriptionStatus.FAILED
        lecture.transcript_metadata = {
            **(lecture.transcript_metadata or {}),
            "error_message": error.message,
            "stage": error.stage,
            "stage_detail": "FAILED",
            "stage_message": error.message,
            "last_activity_at": timed_out_at,
            "stage_trace": [
                *list((lecture.transcript_metadata or {}).get("stage_trace", [])),
                {
                    "stage_detail": "FAILED",
                    "message": error.message,
                    "timestamp": timed_out_at,
                },
            ][-20:],
        }

    try:
        lecture.save(
            update_fields=[
                "status",
                "processing_progress",
                "processing_metadata",
                "transcription_status",
                "transcript_metadata",
                "updated_at",
            ]
        )
    except (OperationalError, DatabaseError) as exc:  # pragma: no cover - depends on local DB backend
        logger.warning(
            "processing_timeout_save_skipped",
            extra={
                "lecture_id": str(lecture.id),
                "reason_code": reason_code,
                "error": str(exc),
            },
        )
        return False

    if lecture.slide_processing_status in ACTIVE_SLIDE_STATUSES:
        from apps.ai_pipeline.services import SlideProcessingService

        SlideProcessingService().fail_pipeline(lecture=lecture, error=error)

    if lecture.audio_processing_status in ACTIVE_AUDIO_STATUSES:
        from apps.audio_pipeline.services import AudioPreprocessingService

        AudioPreprocessingService().fail_pipeline(lecture=lecture, error=error)

    from apps.notes.generation import NoteGenerationOrchestrator

    note_orchestrator = NoteGenerationOrchestrator()
    for note in lecture.generated_notes.exclude(status__in=[NoteGenerationStatus.COMPLETED, NoteGenerationStatus.FAILED]):
        note_orchestrator.fail(lecture=lecture, model_name=note.model_name, error=error)

    usage_log = UsageLogService.get_latest_for_lecture(lecture=lecture)
    if usage_log is not None:
        UsageLogService.fail_processing_once(usage_log=usage_log, error_message=error.message)
    return True


def _mark_timeout_failure(*, lecture: Lecture, error: PipelineError, reason_code: str) -> bool:
    return _mark_processing_failure(lecture=lecture, error=error, reason_code=reason_code)


def cancel_lecture_processing(*, lecture: Lecture, error_message: str, reason_code: str = "admin_cancelled") -> bool:
    error = PipelineError(stage="PROCESSING_CANCELLED", message=error_message)
    return _mark_processing_failure(lecture=lecture, error=error, reason_code=reason_code)


def enforce_lecture_processing_timeouts(*, lecture: Lecture) -> PipelineError | None:
    if lecture.status in {LectureStatus.COMPLETED, LectureStatus.FAILED} and lecture.transcription_status not in ACTIVE_TRANSCRIPTION_STATUSES:
        return None

    now = timezone.now()
    processing_metadata = lecture.processing_metadata or {}
    pipeline_started_at = _parse_datetime(processing_metadata.get("pipeline_started_at"))
    if pipeline_started_at is not None:
        elapsed_seconds = (now - pipeline_started_at).total_seconds()
        if elapsed_seconds > settings.LECTURE_PROCESSING_TIMEOUT_SECONDS:
            error = PipelineError(
                stage="PROCESSING_TIMEOUT",
                message="This processing job timed out after 30 minutes and was cancelled automatically. Please try again later.",
            )
            _mark_timeout_failure(lecture=lecture, error=error, reason_code="overall_processing_timeout")
            return error

    if lecture.transcription_status == TranscriptionStatus.TRANSCRIBING:
        inactivity_seconds = transcription_inactivity_seconds(lecture=lecture)
        if inactivity_seconds is not None and inactivity_seconds > settings.TRANSCRIPTION_CHUNK_INACTIVITY_TIMEOUT_SECONDS:
            error = PipelineError(
                stage="TRANSCRIPTION_TIMEOUT",
                message="Transcription stalled for more than 10 minutes between chunks and was cancelled automatically. Please try again later.",
            )
            _mark_timeout_failure(
                lecture=lecture,
                error=error,
                reason_code="transcription_chunk_inactivity_timeout",
            )
            return error

    active_note_statuses = set(
        lecture.generated_notes.exclude(status__in=[NoteGenerationStatus.COMPLETED, NoteGenerationStatus.FAILED])
        .values_list("status", flat=True)
    )
    if active_note_statuses & ACTIVE_NOTE_STATUSES:
        latest_note_activity = _latest_note_activity_at(lecture)
        if latest_note_activity is not None:
            inactivity_seconds = (now - latest_note_activity).total_seconds()
            if inactivity_seconds > settings.NOTE_GENERATION_INACTIVITY_TIMEOUT_SECONDS:
                error = PipelineError(
                    stage="NOTE_GENERATION_TIMEOUT",
                    message="ساخت جزوه برای مدت طولانی بدون پیشرفت ماند و به‌صورت خودکار متوقف شد. لطفاً کمی بعد دوباره تلاش کنید.",
                )
                _mark_timeout_failure(
                    lecture=lecture,
                    error=error,
                    reason_code="note_generation_inactivity_timeout",
                )
                return error

    return None
