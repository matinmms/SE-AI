from __future__ import annotations

import logging
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from apps.ai_pipeline.exceptions import PipelineError
from apps.audio_pipeline.cleanup.service import TranscriptCleanupService
from apps.audio_pipeline.hotwords.service import HotwordGenerationService
from apps.audio_pipeline.hotwords.stt_prompt import build_stt_context_prompt, stt_context_prompt_enabled
from apps.audio_pipeline.metadata.service import AudioMetadataService
from apps.audio_pipeline.merging.service import TranscriptMergingService
from apps.audio_pipeline.models import AudioChunk, TranscriptChunk
from apps.lectures.language_options import stt_language_code
from apps.audio_pipeline.processors.ffmpeg import FFmpegProcessor
from apps.audio_pipeline.providers.avanegar import AvanegarTranscriptionProvider
from apps.audio_pipeline.providers.base import BaseTranscriptionProvider, TranscriptionPollResult, TranscriptionResult
from apps.lectures.models import Lecture, LectureStatus, TranscriptionStatus
from services.errors.recorder import ErrorRecordingService
from services.lectures.processing import LectureProcessingStateService

logger = logging.getLogger("tesanote")


@contextmanager
def lecture_transcription_cost_context(lecture: Lecture):
    """Attribute every paid STT request to its lecture, user and job.

    The batch submitter runs chunks in a thread pool, where Celery-level
    context vars are not inherited, so attribution must be set here, next to
    the provider call, or the ledger row lands with no lecture and the
    per-process AI cost silently omits transcription.
    """
    from apps.ai_costs.context import avalai_cost_context, current_cost_context
    from apps.ai_pipeline.models import LectureProcessingJob

    context = current_cost_context()
    processing_job_id = context.get("processing_job_id") or (
        LectureProcessingJob.objects.filter(lecture_id=lecture.id)
        .order_by("-created_at")
        .values_list("id", flat=True)
        .first()
    )
    with avalai_cost_context(
        **{
            **context,
            "user_id": str(lecture.user_id) if lecture.user_id else "",
            "lecture_id": str(lecture.id),
            "processing_job_id": str(processing_job_id) if processing_job_id else "",
            "feature": "transcription",
        }
    ):
        yield

MAX_CHUNK_RETRY_ATTEMPTS = 3
MAX_SKIPPED_CHUNK_RATIO = 0.10


class TranscriptionDebugService:
    @staticmethod
    def _is_terminal(chunk: TranscriptChunk) -> bool:
        metadata = chunk.processing_metadata or {}
        poll_status = str(metadata.get("poll_status", "")).upper()
        submission_status = str(metadata.get("submission_status", "")).upper()
        return bool(
            chunk.text.strip()
            or metadata.get("skipped")
            or poll_status in {"COMPLETED", "FAILED", "ERROR"}
            or submission_status in {"COMPLETED", "FAILED", "ERROR"}
        )

    @staticmethod
    def debug_payload(*, lecture: Lecture) -> dict[str, object]:
        chunks = lecture.transcript_chunks.all().order_by("chunk_index")
        processed_chunk_count = sum(1 for chunk in chunks if chunk.provider_job_id or chunk.processing_metadata.get("skipped"))
        completed_chunk_count = sum(1 for chunk in chunks if chunk.text.strip())
        return {
            "lecture_id": str(lecture.id),
            "status": lecture.transcription_status,
            "language": lecture.transcript_language,
            "original_duration": lecture.processing_metadata.get("original_duration", 0.0),
            "optimized_duration": lecture.processing_metadata.get("optimized_duration", 0.0),
            "processed_duration": lecture.transcript_duration_processed,
            "chunk_count": lecture.audio_chunks.count(),
            "processed_chunk_count": processed_chunk_count,
            "estimated_cost": lecture.transcript_cost,
            "estimated_billable_units": lecture.transcript_metadata.get("provider_estimated_billable_units", 0),
            "ideal_billable_units": lecture.transcript_metadata.get("provider_ideal_billable_units", 0),
            "billing_unit_overhead": lecture.transcript_metadata.get("provider_billing_unit_overhead", 0),
            "estimated_savings_percent": lecture.processing_metadata.get("estimated_cost_reduction_percent", 0.0),
            "hotwords": lecture.hotwords_generated,
            "hotword_source": lecture.hotword_source,
            "transcript_provider": lecture.transcript_provider,
            "chunks": [
                {
                    "chunk_index": chunk.chunk_index,
                    "text": chunk.text,
                    "start_ts": chunk.start_ts,
                    "end_ts": chunk.end_ts,
                    "cost": chunk.api_cost,
                    "language": chunk.language,
                    "provider_job_id": chunk.provider_job_id,
                    "provider_trace_id": chunk.provider_trace_id,
                    "hotwords_used": chunk.hotwords_used,
                    "transcript_source": chunk.transcript_source,
                    "estimated_billable_units": chunk.processing_metadata.get("estimated_billable_units", 0),
                    "processing_metadata": chunk.processing_metadata,
                }
                for chunk in chunks
            ],
            "merged_transcript": lecture.merged_transcript,
            "progress": lecture.transcription_progress,
            "metadata": {
                **lecture.transcript_metadata,
                "processed_chunk_count": processed_chunk_count,
                "non_empty_chunk_count": completed_chunk_count,
            },
        }


class TranscriptionOrchestratorService:
    def __init__(self, provider: BaseTranscriptionProvider | None = None) -> None:
        self.provider = provider or AvanegarTranscriptionProvider()
        self.provider_name = getattr(self.provider, "transcript_source", self.provider.__class__.__name__.lower())
        self.cleanup = TranscriptCleanupService()
        self.merging = TranscriptMergingService()
        self.metadata = AudioMetadataService()
        self.hotwords = HotwordGenerationService()
        self.ffmpeg = FFmpegProcessor(ffmpeg_binary=settings.FFMPEG_BINARY, ffprobe_binary=settings.FFPROBE_BINARY)

    @staticmethod
    def _is_chunk_terminal(chunk: TranscriptChunk) -> bool:
        metadata = chunk.processing_metadata or {}
        poll_status = str(metadata.get("poll_status", "")).upper()
        submission_status = str(metadata.get("submission_status", "")).upper()
        retryable = bool(metadata.get("retryable"))
        return bool(
            chunk.text.strip()
            or metadata.get("skipped")
            or poll_status == "COMPLETED"
            or submission_status == "COMPLETED"
            or ((poll_status in {"FAILED", "ERROR"} or submission_status in {"FAILED", "ERROR"}) and not retryable)
        )

    @staticmethod
    def _is_chunk_failed_terminal(chunk: TranscriptChunk) -> bool:
        metadata = chunk.processing_metadata or {}
        poll_status = str(metadata.get("poll_status", "")).upper()
        submission_status = str(metadata.get("submission_status", "")).upper()
        retryable = bool(metadata.get("retryable"))
        return (poll_status in {"FAILED", "ERROR"} or submission_status in {"FAILED", "ERROR"}) and not retryable

    @staticmethod
    def _chunk_retry_attempts(transcript_chunk: TranscriptChunk) -> int:
        return int((transcript_chunk.processing_metadata or {}).get("retry_attempts", 0))

    @staticmethod
    def _audio_chunk_generation_complete(lecture: Lecture) -> bool:
        metadata = lecture.processing_metadata or {}
        if "audio_chunk_generation_complete" not in metadata:
            return True
        return bool(metadata.get("audio_chunk_generation_complete"))

    @staticmethod
    def _is_chunk_stale(chunk: TranscriptChunk, stale_after_seconds: int) -> bool:
        if stale_after_seconds <= 0:
            return False
        metadata = chunk.processing_metadata or {}
        last_markers = [
            metadata.get("last_polled_at"),
            metadata.get("completed_at"),
            metadata.get("submitted_at"),
        ]
        for marker in last_markers:
            if marker in (None, "", 0):
                continue
            try:
                marker_ts = float(marker)
            except (TypeError, ValueError):
                continue
            return (time.time() - marker_ts) > stale_after_seconds
        return False

    def _record_stage_detail(self, *, lecture: Lecture, detail: str, message: str) -> None:
        metadata = lecture.transcript_metadata or {}
        stage_trace = list(metadata.get("stage_trace", []))
        timestamp = timezone.now().isoformat()
        stage_trace.append(
            {
                "stage_detail": detail,
                "message": message,
                "timestamp": timestamp,
            }
        )
        lecture.transcript_metadata = {
            **metadata,
            "stage_detail": detail,
            "stage_message": message,
            "last_progress_activity_at": timestamp,
            "stage_trace": stage_trace[-20:],
        }
        lecture.save(update_fields=["transcript_metadata", "updated_at"])

    @staticmethod
    def _existing_transcript_chunk(*, chunk: AudioChunk) -> TranscriptChunk | None:
        try:
            return chunk.transcript_chunk
        except TranscriptChunk.DoesNotExist:
            return None

    def _touch_progress_activity(self, *, lecture: Lecture) -> None:
        lecture.transcript_metadata = {
            **(lecture.transcript_metadata or {}),
            "last_progress_activity_at": timezone.now().isoformat(),
        }
        lecture.save(update_fields=["transcript_metadata", "updated_at"])

    def start(self, *, lecture: Lecture, reset_existing: bool = False) -> None:
        if not reset_existing and lecture.transcription_status in {
            TranscriptionStatus.QUEUED,
            TranscriptionStatus.TRANSCRIBING,
            TranscriptionStatus.CLEANING,
            TranscriptionStatus.MERGING,
            TranscriptionStatus.COMPLETED,
        } and (lecture.transcript_chunks.exists() or lecture.transcript_metadata):
            return

        LectureProcessingStateService.mark_transcribing(lecture=lecture)
        if reset_existing:
            TranscriptChunk.objects.filter(lecture=lecture).delete()
        hotwords: list[str] = []
        source = "disabled"
        hotword_debug = {
            "slide_terms": [],
            "summarized_terms": [],
            "metadata_terms": [],
            "metadata_summary_terms": [],
            "source": source,
            "disabled": True,
        }
        if stt_context_prompt_enabled():
            # One context sentence plus this lecture's own foreign-language terms (see stt_prompt.py);
            # it travels to the recogniser as the Whisper prompt so Arabic and English words keep
            # their own script instead of becoming Persian-script guesses.
            try:
                context_prompt = build_stt_context_prompt(lecture)
            except Exception:  # never block a transcription on a hint
                logger.exception("stt_context_prompt_failed", extra={"lecture_id": str(lecture.id)})
                context_prompt = ""
            if context_prompt:
                hotwords = [context_prompt]
                source = "context_prompt"
                hotword_debug = {**hotword_debug, "source": source, "disabled": False, "prompt_chars": len(context_prompt)}
        lecture.transcription_status = TranscriptionStatus.QUEUED
        lecture.transcription_progress = 0
        lecture.transcript_cost = 0
        lecture.transcript_language = ""
        lecture.transcript_duration_processed = 0
        lecture.merged_transcript = ""
        lecture.transcript_provider = self.provider_name
        lecture.hotwords_generated = hotwords
        lecture.hotword_source = source
        lecture.transcript_cost_estimation = {
            "estimated_original_duration": lecture.processing_metadata.get("original_duration", 0.0),
            "estimated_optimized_duration": lecture.processing_metadata.get("optimized_duration", 0.0),
            "estimated_savings_percent": lecture.processing_metadata.get("estimated_cost_reduction_percent", 0.0),
        }
        lecture.transcript_debug_data = {
            "hotword_generation": hotword_debug,
        }
        lecture.transcript_metadata = {
            **lecture.transcript_metadata,
            "stage": "QUEUED",
            "stage_detail": "QUEUED",
            "stage_message": "Waiting to start transcription.",
            "last_progress_activity_at": timezone.now().isoformat(),
            "error_message": "",
            "timestamps": [],
            "processed_chunk_count": 0,
            "empty_chunk_count": 0,
            "empty_chunk_indexes": [],
            "skipped_chunk_count": 0,
            "skipped_chunk_indexes": [],
            "max_skipped_chunk_ratio": MAX_SKIPPED_CHUNK_RATIO,
            "provider_request_count": 0,
            "provider_completed_count": 0,
            "provider_pending_count": lecture.audio_chunks.count(),
            "hotwords": hotwords,
            "hotword_source": source,
            "transcript_provider": self.provider_name,
            "stage_trace": [
                {
                    "stage_detail": "QUEUED",
                    "message": "Waiting to start transcription.",
                    "timestamp": time.time(),
                }
            ],
        }
        lecture.save(
            update_fields=[
                "transcription_status",
                "transcription_progress",
                "transcript_cost",
                "transcript_language",
                "transcript_duration_processed",
                "merged_transcript",
                "transcript_metadata",
                "hotwords_generated",
                "hotword_source",
                "transcript_provider",
                "transcript_cost_estimation",
                "transcript_debug_data",
                "updated_at",
            ]
        )

    def _update_progress(self, *, lecture: Lecture, stage: str) -> None:
        LectureProcessingStateService.mark_transcribing(lecture=lecture)
        total_chunks = max(lecture.audio_chunks.count(), 1)
        transcript_chunks = list(lecture.transcript_chunks.all())
        processed_chunk_count = sum(
            1 for chunk in transcript_chunks if chunk.provider_job_id or chunk.processing_metadata.get("skipped")
        )
        completed_chunk_count = sum(1 for chunk in transcript_chunks if self._is_chunk_terminal(chunk))
        skipped_chunk_indexes = sorted(
            chunk.chunk_index
            for chunk in transcript_chunks
            if (chunk.processing_metadata or {}).get("skipped")
            and (chunk.processing_metadata or {}).get("count_towards_skip_limit", True)
        )
        pending_chunk_count = max(0, total_chunks - completed_chunk_count)
        progress = 100 if total_chunks and completed_chunk_count >= total_chunks and self._audio_chunk_generation_complete(lecture) else min(
            99, round((completed_chunk_count / total_chunks) * 100)
        )
        lecture.transcription_status = stage
        lecture.transcription_progress = progress
        lecture.transcript_metadata = {
            **lecture.transcript_metadata,
            "stage": stage,
            "processed_chunk_count": processed_chunk_count,
            "provider_completed_count": completed_chunk_count,
            "provider_pending_count": pending_chunk_count,
            "skipped_chunk_count": len(skipped_chunk_indexes),
            "skipped_chunk_indexes": skipped_chunk_indexes,
            "max_skipped_chunk_ratio": MAX_SKIPPED_CHUNK_RATIO,
        }
        lecture.save(update_fields=["transcription_status", "transcription_progress", "transcript_metadata", "updated_at"])

    def fail(self, *, lecture: Lecture, error: PipelineError) -> None:
        ErrorRecordingService.record_processing_error(
            user=lecture.user,
            lecture=lecture,
            stage=error.stage,
            error_type=error.__class__.__name__,
            error_message=error.message,
        )
        LectureProcessingStateService.mark_failed(lecture=lecture, error_message=error.message)
        lecture.status = LectureStatus.FAILED
        lecture.transcription_status = TranscriptionStatus.FAILED
        lecture.transcript_metadata = {
            **lecture.transcript_metadata,
            "error_message": error.message,
            "stage": error.stage,
            "stage_detail": "PROVIDER_REQUEST_FAILED" if error.stage == "TRANSCRIBING" else error.stage,
            "stage_message": error.message,
            "stage_trace": [
                *(lecture.transcript_metadata.get("stage_trace", [])),
                {
                    "stage_detail": "PROVIDER_REQUEST_FAILED" if error.stage == "TRANSCRIBING" else error.stage,
                    "message": error.message,
                    "timestamp": time.time(),
                },
            ][-20:],
        }
        lecture.save(update_fields=["status", "transcription_status", "transcript_metadata", "updated_at"])

    def _increment_provider_request_count(self, *, lecture: Lecture) -> None:
        lecture.transcript_metadata = {
            **lecture.transcript_metadata,
            "provider_request_count": int(lecture.transcript_metadata.get("provider_request_count", 0)) + 1,
            "last_progress_activity_at": timezone.now().isoformat(),
        }
        lecture.save(update_fields=["transcript_metadata", "updated_at"])

    @contextmanager
    def _materialized_chunk_path(self, *, chunk: AudioChunk):
        from infrastructure.storage.factory import get_storage_service

        try:
            with get_storage_service().materialize_to_temp(chunk.optimized_audio_path) as path:
                yield path
        except Exception as error:
            if isinstance(error, PipelineError):
                raise
            raise PipelineError(stage="TRANSCRIBING", message="Chunk audio could not be prepared for transcription.") from error

    def _complete_if_ready(self, *, lecture: Lecture) -> None:
        if self.ready_for_merge(lecture=lecture):
            self.merge_transcript(lecture=lecture)

    def ready_for_merge(self, *, lecture: Lecture) -> bool:
        if lecture.status == LectureStatus.FAILED or lecture.transcription_status == TranscriptionStatus.FAILED:
            return False
        total_chunks = lecture.audio_chunks.count()
        if total_chunks <= 0 or not self._audio_chunk_generation_complete(lecture):
            return False
        transcript_chunks = list(lecture.transcript_chunks.all())
        if len(transcript_chunks) < total_chunks:
            return False
        if any(self._is_chunk_failed_terminal(chunk) for chunk in transcript_chunks):
            return False
        return all(self._is_chunk_terminal(chunk) for chunk in transcript_chunks)

    def terminal_failure_chunks(self, *, lecture: Lecture) -> list[TranscriptChunk]:
        return [
            chunk
            for chunk in lecture.transcript_chunks.select_related("lecture", "audio_chunk").all().order_by("chunk_index")
            if self._is_chunk_failed_terminal(chunk)
        ]

    def skipped_chunks(self, *, lecture: Lecture) -> list[TranscriptChunk]:
        return [
            chunk
            for chunk in lecture.transcript_chunks.select_related("lecture", "audio_chunk").all().order_by("chunk_index")
            if (chunk.processing_metadata or {}).get("skipped")
        ]

    def budgeted_skipped_chunks(self, *, lecture: Lecture) -> list[TranscriptChunk]:
        return [
            chunk
            for chunk in self.skipped_chunks(lecture=lecture)
            if (chunk.processing_metadata or {}).get("count_towards_skip_limit", True)
        ]

    def _allowed_skipped_chunk_count(self, *, total_chunks: int) -> int:
        return int(total_chunks * MAX_SKIPPED_CHUNK_RATIO)

    def _enforce_skipped_chunk_budget(self, *, lecture: Lecture) -> None:
        total_chunks = lecture.audio_chunks.count()
        if total_chunks <= 0:
            return

        skipped_chunks = self.budgeted_skipped_chunks(lecture=lecture)
        skipped_count = len(skipped_chunks)
        lecture.transcript_metadata = {
            **lecture.transcript_metadata,
            "skipped_chunk_count": skipped_count,
            "skipped_chunk_indexes": [chunk.chunk_index for chunk in skipped_chunks],
            "max_skipped_chunk_ratio": MAX_SKIPPED_CHUNK_RATIO,
        }
        lecture.save(update_fields=["transcript_metadata", "updated_at"])

        if (skipped_count / total_chunks) <= MAX_SKIPPED_CHUNK_RATIO:
            return

        allowed_count = self._allowed_skipped_chunk_count(total_chunks=total_chunks)
        error = PipelineError(
            stage="TRANSCRIBING",
            message=(
                f"Transcription cancelled because {skipped_count} of {total_chunks} chunks were skipped, "
                f"exceeding the allowed limit of {allowed_count} chunk(s) ({int(MAX_SKIPPED_CHUNK_RATIO * 100)}%)."
            ),
        )
        self.fail(lecture=lecture, error=error)

    def pending_poll_chunks(self, *, lecture: Lecture) -> list[TranscriptChunk]:
        return [
            chunk
            for chunk in lecture.transcript_chunks.select_related("lecture", "audio_chunk").all().order_by("chunk_index")
            if not self._is_chunk_terminal(chunk) and bool(chunk.provider_job_id)
        ]

    def pending_submission_placeholders(self, *, lecture: Lecture) -> list[TranscriptChunk]:
        return [
            chunk
            for chunk in lecture.transcript_chunks.select_related("lecture", "audio_chunk").all().order_by("chunk_index")
            if not self._is_chunk_terminal(chunk) and not chunk.provider_job_id
        ]

    def submission_candidates(
        self,
        *,
        lecture: Lecture,
        start_index: int | None = None,
        limit: int | None = None,
        stale_after_seconds: int | None = None,
    ) -> list[AudioChunk]:
        stale_after = stale_after_seconds if stale_after_seconds is not None else settings.TRANSCRIPTION_STUCK_TIMEOUT_SECONDS
        candidates: list[AudioChunk] = []
        queryset = lecture.audio_chunks.all().order_by("chunk_index")
        if start_index is not None:
            queryset = queryset.filter(chunk_index__gte=start_index)
        for chunk in queryset:
            transcript_chunk = self._existing_transcript_chunk(chunk=chunk)
            if transcript_chunk is None:
                candidates.append(chunk)
            elif self._is_chunk_terminal(transcript_chunk):
                continue
            elif self._submission_needs_recovery(transcript_chunk=transcript_chunk):
                candidates.append(chunk)
            elif bool((transcript_chunk.processing_metadata or {}).get("retryable")):
                candidates.append(chunk)
            elif self._is_chunk_stale(transcript_chunk, stale_after_seconds=stale_after):
                candidates.append(chunk)
            if limit and len(candidates) >= limit:
                break
        return candidates

    def _submission_needs_recovery(self, *, transcript_chunk: TranscriptChunk) -> bool:
        metadata = transcript_chunk.processing_metadata or {}
        if transcript_chunk.provider_job_id or transcript_chunk.text.strip():
            return False
        submission_status = str(metadata.get("submission_status", "")).upper()
        if submission_status not in {"SUBMITTING", "FAILED", "ERROR"}:
            return False
        return self._is_chunk_stale(
            transcript_chunk,
            stale_after_seconds=max(5, settings.TRANSCRIPTION_SUBMISSION_STALE_SECONDS),
        )

    def _build_submission_placeholder(self, *, chunk: AudioChunk, hotwords: list[str]) -> TranscriptChunk:
        existing = self._existing_transcript_chunk(chunk=chunk)
        existing_metadata = dict(existing.processing_metadata or {}) if existing is not None else {}
        transcript_chunk, _ = TranscriptChunk.objects.update_or_create(
            audio_chunk=chunk,
            defaults={
                "lecture": chunk.lecture,
                "chunk_index": chunk.chunk_index,
                "text": "",
                "raw_text": "",
                "start_ts": chunk.start_ts,
                "end_ts": chunk.end_ts,
                "language": "",
                "api_cost": 0,
                "processing_time_ms": 0,
                "confidence_score": None,
                "provider_job_id": "",
                "provider_trace_id": "",
                "hotwords_used": hotwords,
                "transcript_source": self.provider_name,
                "processing_metadata": {
                    "submission_status": "SUBMITTING",
                    "submitted_at": time.time(),
                    "hotword_count": len(hotwords),
                    "estimated_billable_units": self.metadata.billable_units_for_duration(
                        duration_seconds=chunk.duration_seconds,
                        billing_unit_seconds=settings.AVANEGAR_BILLING_UNIT_SECONDS,
                    ),
                    "retryable": False,
                    "retry_attempts": int(existing_metadata.get("retry_attempts", 0)),
                    "submit_attempts": int(existing_metadata.get("submit_attempts", 0)),
                },
                "raw_response": {},
            },
        )
        return transcript_chunk

    def mark_submission_failure(self, *, chunk: AudioChunk, error: PipelineError) -> TranscriptChunk:
        hotwords = chunk.lecture.hotwords_generated[: settings.HOTWORDS_MAX_COUNT]
        transcript_chunk = self._build_submission_placeholder(chunk=chunk, hotwords=hotwords)
        metadata = dict(transcript_chunk.processing_metadata or {})
        submit_attempts = int(metadata.get("submit_attempts", 0)) + 1
        metadata["submission_status"] = "FAILED"
        metadata["failure_stage"] = error.stage
        metadata["failure_message"] = error.message
        metadata["failed_at"] = time.time()
        metadata["retryable"] = True
        metadata["submit_attempts"] = submit_attempts
        transcript_chunk.processing_metadata = metadata
        transcript_chunk.raw_response = {
            **(transcript_chunk.raw_response or {}),
            "error": error.message,
            "stage": error.stage,
        }
        transcript_chunk.save(update_fields=["processing_metadata", "raw_response", "updated_at"])
        self._touch_progress_activity(lecture=chunk.lecture)
        return transcript_chunk

    def _begin_retry_attempt(self, *, transcript_chunk: TranscriptChunk) -> TranscriptChunk:
        metadata = dict(transcript_chunk.processing_metadata or {})
        metadata["retry_attempts"] = int(metadata.get("retry_attempts", 0)) + 1
        metadata["last_retry_at"] = timezone.now().isoformat()
        metadata["retryable"] = metadata["retry_attempts"] < MAX_CHUNK_RETRY_ATTEMPTS
        transcript_chunk.processing_metadata = metadata
        transcript_chunk.save(update_fields=["processing_metadata", "updated_at"])
        return transcript_chunk

    def mark_fallback_failure(
        self,
        *,
        transcript_chunk: TranscriptChunk,
        error: PipelineError,
        failure_context: str,
    ) -> TranscriptChunk:
        metadata = dict(transcript_chunk.processing_metadata or {})
        fallback_attempts = int(metadata.get("fallback_attempts", 0)) + 1
        metadata.update(
            {
                "submission_status": "FAILED",
                "retryable": True,
                "failure_stage": error.stage,
                "failure_message": error.message,
                "failed_at": time.time(),
                "fallback_attempts": fallback_attempts,
                "fallback_status": "FAILED",
                "fallback_mode": failure_context,
                "fallback_failure_message": error.message,
                "fallback_failed_at": timezone.now().isoformat(),
            }
        )
        transcript_chunk.processing_metadata = metadata
        transcript_chunk.raw_response = {
            **(transcript_chunk.raw_response or {}),
            "error": error.message,
            "stage": error.stage,
            "fallback_mode": failure_context,
        }
        transcript_chunk.save(update_fields=["processing_metadata", "raw_response", "updated_at"])
        self._touch_progress_activity(lecture=transcript_chunk.lecture)
        return transcript_chunk

    def mark_chunk_skipped(
        self,
        *,
        transcript_chunk: TranscriptChunk,
        reason: str,
        message: str,
    ) -> TranscriptChunk:
        lecture = transcript_chunk.lecture
        metadata = dict(transcript_chunk.processing_metadata or {})
        metadata.update(
            {
                "skipped": True,
                "skip_reason": reason,
                "skip_message": message,
                "skipped_at": timezone.now().isoformat(),
                "count_towards_skip_limit": True,
                "retryable": False,
                "submission_status": "SKIPPED",
                "poll_status": "SKIPPED",
                "failure_message": message,
                "estimated_billable_units": metadata.get(
                    "estimated_billable_units",
                    self.metadata.billable_units_for_duration(
                        duration_seconds=transcript_chunk.audio_chunk.duration_seconds,
                        billing_unit_seconds=settings.AVANEGAR_BILLING_UNIT_SECONDS,
                    ),
                ),
            }
        )
        transcript_chunk.text = ""
        transcript_chunk.raw_text = ""
        transcript_chunk.language = ""
        transcript_chunk.provider_job_id = ""
        transcript_chunk.provider_trace_id = ""
        transcript_chunk.processing_metadata = metadata
        transcript_chunk.raw_response = {
            **(transcript_chunk.raw_response or {}),
            "skipped": True,
            "reason": reason,
            "message": message,
        }
        transcript_chunk.save(
            update_fields=[
                "text",
                "raw_text",
                "language",
                "provider_job_id",
                "provider_trace_id",
                "processing_metadata",
                "raw_response",
                "updated_at",
            ]
        )
        self._record_stage_detail(
            lecture=lecture,
            detail="CHUNK_SKIPPED",
            message=f"Skipped chunk {transcript_chunk.chunk_index + 1} after repeated transcription failures.",
        )
        self._update_progress(lecture=lecture, stage=TranscriptionStatus.TRANSCRIBING)
        self._touch_progress_activity(lecture=lecture)
        logger.warning(
            "transcription_chunk_skipped",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_index": transcript_chunk.chunk_index,
                "retry_attempts": metadata.get("retry_attempts", 0),
                "skip_reason": reason,
            },
        )
        self._enforce_skipped_chunk_budget(lecture=lecture)
        return transcript_chunk

    def _should_use_split_fallback(
        self,
        *,
        chunk: AudioChunk,
        transcript_chunk: TranscriptChunk,
        error: PipelineError,
    ) -> bool:
        metadata = transcript_chunk.processing_metadata or {}
        submit_attempts = int(metadata.get("submit_attempts", 0))
        if submit_attempts < 2:
            return False
        if chunk.duration_seconds < max(12.0, settings.GAPGPT_TRANSCRIPTION_MIN_CHUNK_SECONDS * 2):
            return False
        lowered_message = error.message.lower()
        if any(
            marker in lowered_message
            for marker in (
                "not configured",
                "provider job id",
                "busy handling many",
                "system is busy",
                "timed out",
            )
        ):
            return False
        return True

    def _transcribe_provider_file_sync(self, *, file_path: str, hotwords: list[str]) -> TranscriptionResult:
        submission = self.provider.submit(file_path=file_path, hotwords=hotwords)
        if submission.initial_result is not None:
            return submission.initial_result

        max_attempts = 30
        for _ in range(max_attempts):
            result = self.provider.poll(
                provider_job_id=submission.provider_job_id,
                provider_trace_id=submission.provider_trace_id,
            )
            status = result.status.upper()
            if status in {"QUEUED", "PENDING", "PROCESSING", "RUNNING"}:
                time.sleep(2)
                continue
            if status in {"FAILED", "ERROR"}:
                raise PipelineError(stage="TRANSCRIBING", message="Fallback split transcription failed during provider polling.")
            return TranscriptionResult(
                text=result.text,
                raw_text=result.raw_text,
                language=result.language,
                segments=result.segments,
                confidence_score=result.confidence_score,
                raw_response=result.raw_response,
            )

        raise PipelineError(stage="TRANSCRIBING", message="Fallback split transcription did not complete within the polling window.")

    def _transcribe_with_split_fallback(
        self,
        *,
        chunk: AudioChunk,
        transcript_chunk: TranscriptChunk,
        hotwords: list[str],
        triggering_error: PipelineError,
    ) -> TranscriptChunk:
        lecture = chunk.lecture
        self._record_stage_detail(
            lecture=lecture,
            detail="FALLBACK_SPLIT_TRANSCRIPTION",
            message=f"Retrying chunk {chunk.chunk_index + 1} as smaller pieces after provider failure.",
        )
        logger.warning(
            "transcription_split_fallback_started",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_index": chunk.chunk_index,
                "duration_seconds": chunk.duration_seconds,
            },
        )

        try:
            midpoint = round(chunk.duration_seconds / 2.0, 3)
            split_ranges = [(0.0, midpoint), (midpoint, chunk.duration_seconds)]
            part_results: list[TranscriptionResult] = []

            with self._materialized_chunk_path(chunk=chunk) as source_path, tempfile.TemporaryDirectory(
                prefix=f"transcription-fallback-{chunk.id}-"
            ) as tmp_dir:
                tmp_path = Path(tmp_dir)
                for part_index, (start_ts, end_ts) in enumerate(split_ranges):
                    if (end_ts - start_ts) <= 0.1:
                        continue
                    output_path = str(tmp_path / f"chunk-{chunk.chunk_index:03d}-part-{part_index:02d}.mp3")
                    self.ffmpeg.extract_chunk(
                        input_path=source_path,
                        output_path=output_path,
                        start_ts=start_ts,
                        end_ts=end_ts,
                    )
                    with lecture_transcription_cost_context(lecture):
                        part_results.append(
                            self._transcribe_provider_file_sync(
                                file_path=output_path, hotwords=hotwords
                            )
                        )

            combined_result = TranscriptionResult(
                text="\n".join(result.text.strip() for result in part_results if result.text.strip()).strip(),
                raw_text="\n".join(result.raw_text.strip() for result in part_results if result.raw_text.strip()).strip(),
                language=next((result.language for result in part_results if result.language.strip()), ""),
                segments=[],
                confidence_score=None,
                raw_response={
                    "fallback_mode": "split_submit_failure",
                    "fallback_part_count": len(part_results),
                    "triggering_error": triggering_error.message,
                    "parts": [result.raw_response for result in part_results],
                },
            )

            transcript_chunk.provider_job_id = transcript_chunk.provider_job_id or f"fallback-split-{chunk.id}"
            transcript_chunk.provider_trace_id = ""
            transcript_chunk.save(update_fields=["provider_job_id", "provider_trace_id", "updated_at"])

            transcript_chunk = self._persist_result(
                transcript_chunk=transcript_chunk,
                result=combined_result,
                processing_metadata={
                    "submission_status": "COMPLETED",
                    "poll_status": "COMPLETED",
                    "retryable": False,
                    "fallback_mode": "split_submit_failure",
                    "fallback_part_count": len(part_results),
                    "fallback_triggering_error": triggering_error.message,
                },
            )
            self._record_stage_detail(
                lecture=lecture,
                detail="PROVIDER_RESPONSE_RECEIVED",
                message=f"Recovered transcript for chunk {chunk.chunk_index + 1} via split fallback.",
            )
            logger.info(
                "transcription_split_fallback_completed",
                extra={
                    "lecture_id": str(lecture.id),
                    "chunk_index": chunk.chunk_index,
                    "fallback_part_count": len(part_results),
                },
            )
            return transcript_chunk
        except PipelineError as error:
            if self._chunk_retry_attempts(transcript_chunk) >= MAX_CHUNK_RETRY_ATTEMPTS:
                return self.mark_chunk_skipped(
                    transcript_chunk=transcript_chunk,
                    reason="retry_limit_exceeded",
                    message=(
                        f"Chunk {chunk.chunk_index + 1} was skipped after {MAX_CHUNK_RETRY_ATTEMPTS} failed retry attempts. "
                        f"Last error: {error.message}"
                    ),
                )
            self.mark_fallback_failure(
                transcript_chunk=transcript_chunk,
                error=error,
                failure_context="split_submit_failure",
            )
            logger.warning(
                "transcription_split_fallback_failed",
                extra={
                    "lecture_id": str(lecture.id),
                    "chunk_index": chunk.chunk_index,
                    "error_message": error.message,
                },
            )
            raise

    def count_remaining_submission_candidates(self, *, lecture: Lecture, start_index: int | None = None) -> int:
        return len(self.submission_candidates(lecture=lecture, start_index=start_index, limit=None))

    def _persist_result(
        self,
        *,
        transcript_chunk: TranscriptChunk,
        result: TranscriptionResult | TranscriptionPollResult,
        processing_metadata: dict[str, object] | None = None,
    ) -> TranscriptChunk:
        lecture = transcript_chunk.lecture
        cleaned_text = self.cleanup.clean(text=result.text) if result.text.strip() else ""
        metadata = {
            **transcript_chunk.processing_metadata,
            **(processing_metadata or {}),
        }
        metadata["estimated_billable_units"] = self.metadata.billable_units_for_duration(
            duration_seconds=transcript_chunk.audio_chunk.duration_seconds,
            billing_unit_seconds=settings.AVANEGAR_BILLING_UNIT_SECONDS,
        )
        metadata["completed_at"] = timezone.now().isoformat()
        metadata["poll_status"] = "COMPLETED"
        provider_name = getattr(result, "provider", "") or self.provider_name
        provider_model = getattr(result, "model", "") or metadata.get("transcription_model", "")
        fallback_used = bool(getattr(result, "fallback_used", False))
        provider_chunks_count = int(getattr(result, "chunks_count", 1) or 1)
        provider_latency_ms = int(getattr(result, "latency_ms", 0) or 0)
        audio_size_bytes = int(getattr(result, "audio_size_bytes", 0) or 0)
        provider_metadata = getattr(result, "metadata", {}) or {}
        metadata.update(
            {
                "transcription_provider": provider_name,
                "transcription_model": provider_model,
                "fallback_used": fallback_used,
                "provider_chunks_count": provider_chunks_count,
                "provider_latency_ms": provider_latency_ms,
                "audio_size_bytes": audio_size_bytes,
            }
        )
        if provider_metadata:
            metadata["provider_metadata"] = provider_metadata
        if not cleaned_text:
            empty_indexes = list(lecture.transcript_metadata.get("empty_chunk_indexes", []))
            if transcript_chunk.chunk_index not in empty_indexes:
                empty_indexes.append(transcript_chunk.chunk_index)
            lecture.transcript_metadata = {
                **lecture.transcript_metadata,
                "empty_chunk_indexes": sorted(empty_indexes),
                "empty_chunk_count": len(empty_indexes),
            }
            lecture.save(update_fields=["transcript_metadata", "updated_at"])

        transcript_chunk.text = cleaned_text
        transcript_chunk.raw_text = result.raw_text
        transcript_chunk.language = result.language
        transcript_chunk.confidence_score = result.confidence_score
        transcript_chunk.transcript_source = provider_name
        transcript_chunk.processing_metadata = metadata
        transcript_chunk.raw_response = result.raw_response
        transcript_chunk.api_cost = round((transcript_chunk.audio_chunk.duration_seconds / 60.0) * settings.GAPGPT_WHISPER_PRICE_PER_MINUTE, 6)
        transcript_chunk.processing_time_ms = int(
            max(0.0, time.time() - float(transcript_chunk.processing_metadata.get("submitted_at", time.time()))) * 1000
        )
        transcript_chunk.save(
            update_fields=[
                "text",
                "raw_text",
                "language",
                "confidence_score",
                "transcript_source",
                "processing_metadata",
                "raw_response",
                "api_cost",
                "processing_time_ms",
                "updated_at",
            ]
        )
        lecture.transcript_provider = provider_name
        lecture.save(update_fields=["transcript_provider", "updated_at"])
        from services.note_review.capture import has_recognizer_stats, persist_segments

        if has_recognizer_stats(getattr(result, "segments", None)):
            persist_segments(audio_chunk=transcript_chunk.audio_chunk, segments=result.segments)
        self._update_progress(lecture=lecture, stage=TranscriptionStatus.TRANSCRIBING)
        self._touch_progress_activity(lecture=lecture)
        logger.info(
            "transcript_chunk_saved",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_index": transcript_chunk.chunk_index,
                "provider": provider_name,
            },
        )
        return transcript_chunk

    def _skip_short_chunk(self, *, chunk: AudioChunk) -> TranscriptChunk:
        lecture = chunk.lecture
        transcript_chunk, _ = TranscriptChunk.objects.update_or_create(
            audio_chunk=chunk,
            defaults={
                "lecture": lecture,
                "chunk_index": chunk.chunk_index,
                "text": "",
                "raw_text": "",
                "start_ts": chunk.start_ts,
                "end_ts": chunk.end_ts,
                "language": "",
                "api_cost": 0,
                "processing_time_ms": 0,
                "confidence_score": None,
                "provider_job_id": "",
                "provider_trace_id": "",
                "hotwords_used": lecture.hotwords_generated[: settings.HOTWORDS_MAX_COUNT],
                "transcript_source": self.provider_name,
                "processing_metadata": {
                    "skipped": True,
                    "reason": "chunk_too_short",
                    "count_towards_skip_limit": False,
                    "submitted_at": time.time(),
                    "estimated_billable_units": 0,
                    "poll_status": "COMPLETED",
                },
                "raw_response": {
                    "skipped": True,
                    "reason": "chunk_too_short",
                },
            },
        )
        self._update_progress(lecture=lecture, stage=TranscriptionStatus.TRANSCRIBING)
        self._touch_progress_activity(lecture=lecture)
        self._enforce_skipped_chunk_budget(lecture=lecture)
        return transcript_chunk

    def _record_sync_failure(self, *, transcript_chunk: TranscriptChunk, error: PipelineError) -> None:
        transcript_chunk.processing_metadata = {
            **transcript_chunk.processing_metadata,
            "failed": True,
            "failure_stage": error.stage,
            "failure_message": error.message,
            "poll_status": "FAILED",
        }
        transcript_chunk.raw_response = {
            **transcript_chunk.raw_response,
            "error": error.message,
            "stage": error.stage,
        }
        transcript_chunk.save(update_fields=["processing_metadata", "raw_response", "updated_at"])

    def _legacy_sync_result(self, *, chunk: AudioChunk) -> TranscriptionResult:
        if not hasattr(self.provider, "transcribe"):
            raise PipelineError(stage="TRANSCRIBING", message="Configured provider does not support synchronous transcription fallback.")
        hotwords = chunk.lecture.hotwords_generated[: settings.HOTWORDS_MAX_COUNT]
        language = stt_language_code(chunk.lecture.input_language)
        with self._materialized_chunk_path(chunk=chunk) as file_path:
            try:
                result = self.provider.transcribe(
                    file_path=file_path,
                    hotwords=hotwords,
                    language=language,
                )
            except TypeError:
                result = self.provider.transcribe(file_path=file_path)
        if not isinstance(result, TranscriptionResult):
            raise PipelineError(stage="TRANSCRIBING", message="Legacy provider returned an unexpected transcription result.")
        return result

    def submit_chunk(self, *, chunk: AudioChunk) -> TranscriptChunk:
        lecture = chunk.lecture
        existing = self._existing_transcript_chunk(chunk=chunk)
        if existing is not None:
            if self._is_chunk_terminal(existing):
                return existing
            should_retry = (
                self._submission_needs_recovery(transcript_chunk=existing)
                or bool((existing.processing_metadata or {}).get("retryable"))
                or self._is_chunk_stale(existing, stale_after_seconds=settings.TRANSCRIPTION_STUCK_TIMEOUT_SECONDS)
            )
            if not should_retry:
                return existing
            if self._chunk_retry_attempts(existing) >= MAX_CHUNK_RETRY_ATTEMPTS:
                return self.mark_chunk_skipped(
                    transcript_chunk=existing,
                    reason="retry_limit_exceeded",
                    message=(
                        f"Chunk {chunk.chunk_index + 1} was skipped after {MAX_CHUNK_RETRY_ATTEMPTS} failed retry attempts."
                    ),
                )
            existing = self._begin_retry_attempt(transcript_chunk=existing)

        lecture.transcription_status = TranscriptionStatus.TRANSCRIBING
        lecture.transcript_metadata = {
            **lecture.transcript_metadata,
            "stage": TranscriptionStatus.TRANSCRIBING,
        }
        lecture.save(update_fields=["transcription_status", "transcript_metadata", "updated_at"])

        hotwords = lecture.hotwords_generated[: settings.HOTWORDS_MAX_COUNT]
        language = stt_language_code(lecture.input_language)
        self._record_stage_detail(
            lecture=lecture,
            detail="REQUESTING_PROVIDER",
            message=f"Submitting chunk {chunk.chunk_index + 1} to {self.provider_name}.",
        )
        self._increment_provider_request_count(lecture=lecture)
        logger.info(
            "transcription_submit_started",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_index": chunk.chunk_index,
                "provider": self.provider_name,
            },
        )
        transcript_chunk = self._build_submission_placeholder(chunk=chunk, hotwords=hotwords)
        try:
            with (
                self._materialized_chunk_path(chunk=chunk) as file_path,
                lecture_transcription_cost_context(chunk.lecture),
            ):
                submission = self.provider.submit(
                    file_path=file_path,
                    hotwords=hotwords,
                    language=language,
                )
        except PipelineError as error:
            failed_chunk = self.mark_submission_failure(chunk=chunk, error=error)
            if self._chunk_retry_attempts(failed_chunk) >= MAX_CHUNK_RETRY_ATTEMPTS:
                return self.mark_chunk_skipped(
                    transcript_chunk=failed_chunk,
                    reason="retry_limit_exceeded",
                    message=(
                        f"Chunk {chunk.chunk_index + 1} was skipped after {MAX_CHUNK_RETRY_ATTEMPTS} failed retry attempts. "
                        f"Last error: {error.message}"
                    ),
                )
            if self._should_use_split_fallback(
                chunk=chunk,
                transcript_chunk=failed_chunk,
                error=error,
            ):
                return self._transcribe_with_split_fallback(
                    chunk=chunk,
                    transcript_chunk=failed_chunk,
                    hotwords=hotwords,
                    triggering_error=error,
                )
            raise

        transcript_chunk.provider_job_id = submission.provider_job_id
        transcript_chunk.provider_trace_id = submission.provider_trace_id
        transcript_chunk.hotwords_used = hotwords
        transcript_chunk.transcript_source = self.provider_name
        transcript_chunk.processing_metadata = {
            **(transcript_chunk.processing_metadata or {}),
            "submission_status": submission.status,
            "submitted_at": time.time(),
            "hotword_count": len(hotwords),
            "language": language,
            "estimated_billable_units": self.metadata.billable_units_for_duration(
                duration_seconds=chunk.duration_seconds,
                billing_unit_seconds=settings.AVANEGAR_BILLING_UNIT_SECONDS,
            ),
            "retryable": False,
        }
        transcript_chunk.raw_response = submission.raw_response
        transcript_chunk.save(
            update_fields=[
                "provider_job_id",
                "provider_trace_id",
                "hotwords_used",
                "transcript_source",
                "processing_metadata",
                "raw_response",
                "updated_at",
            ]
        )
        if submission.initial_result is not None:
            transcript_chunk = self._persist_result(
                transcript_chunk=transcript_chunk,
                result=submission.initial_result,
                processing_metadata={
                    "submission_status": "COMPLETED",
                    "poll_status": "COMPLETED",
                },
            )
            self._record_stage_detail(
                lecture=lecture,
                detail="PROVIDER_RESPONSE_RECEIVED",
                message=f"Received transcript for chunk {chunk.chunk_index + 1} from {self.provider_name}.",
            )
        self._update_progress(lecture=lecture, stage=TranscriptionStatus.TRANSCRIBING)
        logger.info(
            "transcription_submit_completed",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_index": chunk.chunk_index,
                "provider": self.provider_name,
                "provider_job_id": transcript_chunk.provider_job_id,
            },
        )
        return transcript_chunk

    def transcribe_chunk(self, *, chunk: AudioChunk) -> TranscriptChunk:
        lecture = chunk.lecture
        if chunk.duration_seconds < settings.GAPGPT_TRANSCRIPTION_MIN_CHUNK_SECONDS:
            transcript_chunk = self._skip_short_chunk(chunk=chunk)
            self._complete_if_ready(lecture=lecture)
            return transcript_chunk

        if hasattr(self.provider, "transcribe"):
            lecture.transcription_status = TranscriptionStatus.TRANSCRIBING
            lecture.save(update_fields=["transcription_status", "updated_at"])
            self._record_stage_detail(
                lecture=lecture,
                detail="REQUESTING_PROVIDER",
                message=f"Submitting chunk {chunk.chunk_index + 1} to {self.provider_name}.",
            )
            self._increment_provider_request_count(lecture=lecture)
            transcript_chunk, _ = TranscriptChunk.objects.update_or_create(
                audio_chunk=chunk,
                defaults={
                    "lecture": lecture,
                    "chunk_index": chunk.chunk_index,
                    "text": "",
                    "raw_text": "",
                    "start_ts": chunk.start_ts,
                    "end_ts": chunk.end_ts,
                    "language": "",
                    "api_cost": 0,
                    "processing_time_ms": 0,
                    "confidence_score": None,
                    "provider_job_id": f"sync-{chunk.id}",
                    "provider_trace_id": "",
                    "hotwords_used": lecture.hotwords_generated[: settings.HOTWORDS_MAX_COUNT],
                    "transcript_source": self.provider_name,
                    "processing_metadata": {
                        "submission_status": "COMPLETED",
                        "submitted_at": time.time(),
                        "hotword_count": len(lecture.hotwords_generated[: settings.HOTWORDS_MAX_COUNT]),
                        "estimated_billable_units": self.metadata.billable_units_for_duration(
                            duration_seconds=chunk.duration_seconds,
                            billing_unit_seconds=settings.AVANEGAR_BILLING_UNIT_SECONDS,
                        ),
                    },
                    "raw_response": {},
                },
            )
            try:
                result = self._legacy_sync_result(chunk=chunk)
            except PipelineError as error:
                self._record_sync_failure(transcript_chunk=transcript_chunk, error=error)
                raise
            transcript_chunk = self._persist_result(
                transcript_chunk=transcript_chunk,
                result=result,
                processing_metadata={"poll_status": "COMPLETED"},
            )
            self._record_stage_detail(
                lecture=lecture,
                detail="PROVIDER_RESPONSE_RECEIVED",
                message=f"Received transcript for chunk {chunk.chunk_index + 1} from {self.provider_name}.",
            )
            self._complete_if_ready(lecture=lecture)
            return transcript_chunk

        transcript_chunk = self.submit_chunk(chunk=chunk)
        max_attempts = 20
        for _ in range(max_attempts):
            completed, failed = self.poll_chunk(transcript_chunk=transcript_chunk)
            transcript_chunk.refresh_from_db()
            if failed:
                raise PipelineError(stage="TRANSCRIBING", message="Async transcription provider reported a chunk failure.")
            if completed:
                self._complete_if_ready(lecture=lecture)
                return transcript_chunk
            time.sleep(1)

        raise PipelineError(stage="TRANSCRIBING", message="Async transcription provider did not finish within the local polling window.")

    def poll_chunk(self, *, transcript_chunk: TranscriptChunk) -> tuple[bool, bool]:
        lecture = transcript_chunk.lecture
        if self._is_chunk_terminal(transcript_chunk):
            metadata = transcript_chunk.processing_metadata or {}
            poll_status = str(metadata.get("poll_status", "")).upper()
            submission_status = str(metadata.get("submission_status", "")).upper()
            self._update_progress(lecture=lecture, stage=TranscriptionStatus.TRANSCRIBING)
            if poll_status in {"FAILED", "ERROR"} or submission_status in {"FAILED", "ERROR"}:
                return True, True
            return True, False
        if not transcript_chunk.provider_job_id:
            raise PipelineError(stage="TRANSCRIBING", message="Provider job id is missing for transcript chunk polling.")

        logger.info(
            "transcription_poll_started",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_index": transcript_chunk.chunk_index,
                "provider": self.provider_name,
            },
        )
        result = self.provider.poll(
            provider_job_id=transcript_chunk.provider_job_id,
            provider_trace_id=transcript_chunk.provider_trace_id,
        )
        status = result.status.upper()
        metadata = {
            **transcript_chunk.processing_metadata,
            **result.processing_metadata,
            "poll_status": status,
            "last_polled_at": time.time(),
        }
        if status in {"QUEUED", "PENDING", "PROCESSING", "RUNNING"}:
            transcript_chunk.processing_metadata = metadata
            transcript_chunk.save(update_fields=["processing_metadata", "updated_at"])
            self._update_progress(lecture=lecture, stage=TranscriptionStatus.TRANSCRIBING)
            logger.info(
                "transcription_poll_pending",
                extra={
                    "lecture_id": str(lecture.id),
                    "chunk_index": transcript_chunk.chunk_index,
                    "provider": self.provider_name,
                },
            )
            return False, False

        if status in {"FAILED", "ERROR"}:
            failure_message = result.raw_response.get("error") or result.raw_response.get("message") or f"Provider polling returned {status}."
            if self._chunk_retry_attempts(transcript_chunk) >= MAX_CHUNK_RETRY_ATTEMPTS:
                self.mark_chunk_skipped(
                    transcript_chunk=transcript_chunk,
                    reason="retry_limit_exceeded",
                    message=(
                        f"Chunk {transcript_chunk.chunk_index + 1} was skipped after {MAX_CHUNK_RETRY_ATTEMPTS} failed retry attempts. "
                        f"Last error: {failure_message}"
                    ),
                )
                return True, False
            poll_failures = int(metadata.get("poll_failures", 0)) + 1
            metadata["poll_failures"] = poll_failures
            metadata["failure_message"] = failure_message
            metadata["retryable"] = True
            transcript_chunk.processing_metadata = metadata
            transcript_chunk.raw_response = result.raw_response
            transcript_chunk.save(update_fields=["processing_metadata", "raw_response", "updated_at"])
            logger.warning(
                "transcription_poll_failed",
                extra={
                    "lecture_id": str(lecture.id),
                    "chunk_index": transcript_chunk.chunk_index,
                    "provider": self.provider_name,
                },
            )
            return True, True

        self._persist_result(
            transcript_chunk=transcript_chunk,
            result=result,
            processing_metadata=metadata,
        )
        logger.info(
            "transcription_poll_completed",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_index": transcript_chunk.chunk_index,
                "provider": self.provider_name,
            },
        )
        return True, False

    def merge_transcript(self, *, lecture: Lecture) -> None:
        if not self.ready_for_merge(lecture=lecture):
            return
        self._record_stage_detail(
            lecture=lecture,
            detail="CLEANING_TRANSCRIPT",
            message="Cleaning transcript chunks before merging.",
        )
        lecture.transcription_status = TranscriptionStatus.CLEANING
        lecture.save(update_fields=["transcription_status", "updated_at"])
        chunks = list(lecture.transcript_chunks.all().order_by("chunk_index"))
        if not chunks:
            raise PipelineError(stage="MERGING", message="No transcript chunks were generated.")

        self._record_stage_detail(
            lecture=lecture,
            detail="MERGING_TRANSCRIPT",
            message="Merging cleaned transcript chunks into the final transcript.",
        )
        lecture.transcription_status = TranscriptionStatus.MERGING
        lecture.save(update_fields=["transcription_status", "updated_at"])
        self._touch_progress_activity(lecture=lecture)
        full_transcript, timestamps, language = self.merging.merge(chunks=chunks)
        provider_request_count = int(lecture.transcript_metadata.get("provider_request_count", 0))
        processed_duration = round(sum(chunk.audio_chunk.duration_seconds for chunk in chunks), 3)
        providers_used = sorted(
            {
                str((chunk.processing_metadata or {}).get("transcription_provider") or chunk.transcript_source or "").strip()
                for chunk in chunks
                if str((chunk.processing_metadata or {}).get("transcription_provider") or chunk.transcript_source or "").strip()
            }
        )
        models_used = sorted(
            {
                str((chunk.processing_metadata or {}).get("transcription_model") or "").strip()
                for chunk in chunks
                if str((chunk.processing_metadata or {}).get("transcription_model") or "").strip()
            }
        )
        provider_chunks_count = sum(
            int((chunk.processing_metadata or {}).get("provider_chunks_count", 1) or 1)
            for chunk in chunks
        )
        provider_latency_ms = sum(
            int((chunk.processing_metadata or {}).get("provider_latency_ms", 0) or 0)
            for chunk in chunks
        )
        audio_size_bytes = sum(
            int((chunk.processing_metadata or {}).get("audio_size_bytes", 0) or 0)
            for chunk in chunks
        )
        fallback_used = any(bool((chunk.processing_metadata or {}).get("fallback_used")) for chunk in chunks)
        estimated_billable_units = self.metadata.billable_units_for_chunks(
            chunk_durations=[chunk.audio_chunk.duration_seconds for chunk in chunks],
            billing_unit_seconds=settings.AVANEGAR_BILLING_UNIT_SECONDS,
        )
        ideal_billable_units = self.metadata.billable_units_for_duration(
            duration_seconds=processed_duration,
            billing_unit_seconds=settings.AVANEGAR_BILLING_UNIT_SECONDS,
        )
        cost_breakdown = self.metadata.estimated_transcription_cost(
            processed_duration=processed_duration,
            request_count=provider_request_count,
        )
        total_cost = cost_breakdown["billed_cost"]
        non_empty_chunk_count = sum(1 for chunk in chunks if chunk.text.strip())
        skipped_chunk_indexes = sorted(
            chunk.chunk_index
            for chunk in chunks
            if (chunk.processing_metadata or {}).get("skipped")
            and (chunk.processing_metadata or {}).get("count_towards_skip_limit", True)
        )

        lecture.transcription_status = TranscriptionStatus.COMPLETED
        lecture.transcription_progress = 100
        lecture.transcript_cost = total_cost
        lecture.transcript_language = language
        lecture.transcript_duration_processed = processed_duration
        lecture.merged_transcript = full_transcript
        lecture.transcript_provider = ",".join(providers_used)
        lecture.transcript_cost_estimation = {
            **lecture.transcript_cost_estimation,
            **cost_breakdown,
            "processed_duration": processed_duration,
        }
        lecture.transcript_debug_data = {
            **lecture.transcript_debug_data,
            "provider_metadata": [chunk.processing_metadata for chunk in chunks],
        }
        lecture.transcript_metadata = {
            **lecture.transcript_metadata,
            "stage": "COMPLETED",
            "stage_detail": "COMPLETED",
            "stage_message": "Transcription completed successfully.",
            "last_progress_activity_at": timezone.now().isoformat(),
            "error_message": "",
            "timestamps": timestamps,
            "processed_chunk_count": len(chunks),
            "processed_duration": processed_duration,
            "estimated_cost": total_cost,
            "non_empty_chunk_count": non_empty_chunk_count,
            "skipped_chunk_count": len(skipped_chunk_indexes),
            "skipped_chunk_indexes": skipped_chunk_indexes,
            "max_skipped_chunk_ratio": MAX_SKIPPED_CHUNK_RATIO,
            "provider_variable_cost": cost_breakdown["variable_cost"],
            "provider_job_floor_cost": cost_breakdown["job_floor_cost"],
            "provider_billed_cost": cost_breakdown["billed_cost"],
            "provider_request_count": provider_request_count,
            "provider_estimated_billable_units": estimated_billable_units,
            "provider_ideal_billable_units": ideal_billable_units,
            "provider_billing_unit_overhead": max(0, estimated_billable_units - ideal_billable_units),
            "providers_used": providers_used,
            "models_used": models_used,
            "fallback_used": fallback_used,
            "transcription_provider_used": providers_used[0] if len(providers_used) == 1 else ",".join(providers_used),
            "transcription_model_used": models_used[0] if len(models_used) == 1 else ",".join(models_used),
            "transcription_chunks_count": provider_chunks_count,
            "transcription_latency_ms": provider_latency_ms,
            "transcription_audio_seconds": processed_duration,
            "transcription_audio_size_mb": round(audio_size_bytes / (1024 * 1024), 3) if audio_size_bytes else 0.0,
            "input_language": lecture.input_language,
            "transcription_language_used": stt_language_code(lecture.input_language),
            "stage_trace": [
                *(lecture.transcript_metadata.get("stage_trace", [])),
                {
                    "stage_detail": "COMPLETED",
                    "message": "Transcription completed successfully.",
                    "timestamp": time.time(),
                },
            ][-20:],
        }
        lecture.save(
            update_fields=[
                "status",
                "transcription_status",
                "transcription_progress",
                "transcript_cost",
                "transcript_language",
                "transcript_duration_processed",
                "merged_transcript",
                "transcript_provider",
                "transcript_metadata",
                "transcript_cost_estimation",
                "transcript_debug_data",
                "updated_at",
            ]
        )
        logger.info(
            "transcription_merge_completed",
            extra={
                "lecture_id": str(lecture.id),
                "chunk_count": len(chunks),
                "processed_duration": processed_duration,
            },
        )
