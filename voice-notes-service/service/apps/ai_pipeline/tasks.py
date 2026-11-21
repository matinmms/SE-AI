from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4

from celery import current_app, shared_task
from django.conf import settings
from django.db import close_old_connections, transaction

from apps.ai_pipeline.exceptions import PipelineError
from apps.ai_pipeline.heartbeat import HeartbeatService
from apps.ai_pipeline.jobs import ProcessingJobService
from apps.ai_pipeline.services import LectureProcessingOrchestrator, SlideProcessingService
from apps.ai_pipeline.stage_metrics import StageConcurrencyStore
from apps.ai_pipeline.timeouts import (
    enforce_lecture_processing_timeouts,
    transcription_inactivity_seconds,
)
from apps.audio_pipeline.models import AudioChunk
from apps.audio_pipeline.providers.router_provider import RoutedTranscriptionProvider
from apps.audio_pipeline.transcription.service import TranscriptionOrchestratorService
from apps.lectures.models import Lecture, LectureProcessingState, LectureStatus, TranscriptionStatus
from apps.notes.tasks import run_note_generation_sync
from infrastructure.observability.scaling import measure_scaling_stage
from infrastructure.queue.control import lecture_task_lock
from infrastructure.storage.base import StorageProviderError

logger = logging.getLogger("celery")


@shared_task(ignore_result=True)
def dispatch_available_processing_jobs() -> dict[str, int]:
    """Event-driven and periodic fair-scheduler reconciliation entry point."""
    from apps.ai_pipeline.scheduler import FairSchedulerService

    return FairSchedulerService.dispatch_available_jobs()


@shared_task(ignore_result=True)
def sample_processing_capacity_resources() -> dict[str, object]:
    """Low-overhead host sampler; the scheduler reads its cached output."""
    from apps.ai_pipeline.capacity import ResourceSampler

    snapshot = ResourceSampler.sample_and_store()
    return snapshot.as_payload() if snapshot else {"status": "unavailable"}


def build_transcription_service() -> TranscriptionOrchestratorService:
    return TranscriptionOrchestratorService(provider=RoutedTranscriptionProvider())


def enqueue_transcription_batch(
    *,
    lecture_id: str,
    start_index: int | None = None,
    limit: int | None = None,
    countdown: int | None = None,
) -> None:
    task_id = str(uuid4())

    def _send() -> None:
        StageConcurrencyStore.record_enqueued(queue="transcription", task_id=task_id)
        current_app.send_task(
            "apps.ai_pipeline.tasks.transcribe_chunks_batch",
            args=[lecture_id, start_index, limit],
            queue="transcription",
            countdown=countdown,
            task_id=task_id,
        )

    transaction.on_commit(_send)


def enqueue_transcription_poll(
    *, lecture_id: str, attempt: int = 0, countdown: int | None = None
) -> None:
    def _send() -> None:
        current_app.send_task(
            "apps.ai_pipeline.tasks.poll_transcription_chunks_batch",
            args=[lecture_id, attempt],
            queue="transcription",
            countdown=countdown,
        )

    transaction.on_commit(_send)


def enqueue_slide_processing(*, lecture_id: str) -> None:
    def _send() -> None:
        current_app.send_task(
            "apps.ai_pipeline.tasks.process_slides_pipeline",
            args=[lecture_id],
            queue="slides",
        )

    transaction.on_commit(_send)


def enqueue_note_generation(*, lecture_id: str) -> None:
    task_id = str(uuid4())

    def _send() -> None:
        StageConcurrencyStore.record_enqueued(queue="notes", task_id=task_id)
        current_app.send_task(
            "apps.notes.tasks.generate_note_task",
            args=[lecture_id],
            queue="notes",
            task_id=task_id,
        )

    transaction.on_commit(_send)


def dispatch_note_generation_if_needed(*, lecture: Lecture) -> dict[str, object]:
    lecture.refresh_from_db()
    if (
        lecture.transcription_status != TranscriptionStatus.COMPLETED
        or not lecture.merged_transcript.strip()
    ):
        return {"status": lecture.transcription_status}
    return LectureProcessingOrchestrator().dispatch_note_generation(lecture=lecture)


def lecture_transcription_failed(*, lecture: Lecture) -> bool:
    return lecture.status == "FAILED" or lecture.transcription_status == TranscriptionStatus.FAILED


def lecture_processing_stopped(*, lecture: Lecture) -> bool:
    return (
        lecture.status in {LectureStatus.FAILED, LectureStatus.COMPLETED}
        or lecture.processing_state == LectureProcessingState.FAILED
    )


def _transcription_submit_parallelism(*, chunk_count: int) -> int:
    return max(
        1,
        min(
            chunk_count,
            int(getattr(settings, "TRANSCRIPTION_SUBMIT_PARALLELISM", 1) or 1),
            int(getattr(settings, "AVALAI_PROVIDER_MAX_CONCURRENCY", 1) or 1),
        ),
    )


def _submit_single_transcription_chunk(*, lecture_id: str, chunk_id: str) -> dict[str, object]:
    close_old_connections()
    try:
        lecture = Lecture.objects.get(id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}

        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            return {"lecture_id": lecture_id, "status": "FAILED", "reason": timeout_error.message}

        chunk = AudioChunk.objects.select_related("lecture").get(id=chunk_id)
        build_transcription_service().submit_chunk(chunk=chunk)
        return {
            "lecture_id": lecture_id,
            "chunk_index": chunk.chunk_index,
            "status": "SUBMITTED",
        }
    except PipelineError as error:
        return {
            "lecture_id": lecture_id,
            "status": "FAILED",
            "stage": error.stage,
            "reason": error.message,
            "chunk_id": chunk_id,
        }
    finally:
        close_old_connections()


def _submit_transcription_chunks(
    *, lecture_id: str, chunks: list[AudioChunk]
) -> list[dict[str, object]]:
    if not chunks:
        return []

    parallelism = _transcription_submit_parallelism(chunk_count=len(chunks))
    if parallelism <= 1:
        return [
            _submit_single_transcription_chunk(lecture_id=lecture_id, chunk_id=str(chunk.id))
            for chunk in chunks
        ]

    logger.info(
        "transcription_parallel_submission_started",
        extra={
            "lecture_id": lecture_id,
            "chunk_count": len(chunks),
            "parallelism": parallelism,
        },
    )
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="stt-submit") as executor:
        futures = [
            executor.submit(
                _submit_single_transcription_chunk, lecture_id=lecture_id, chunk_id=str(chunk.id)
            )
            for chunk in chunks
        ]
        for future in as_completed(futures):
            results.append(future.result())
    return results


def run_transcription_batch_sync(
    *, lecture_id: str, start_index: int | None = None, limit: int | None = None
) -> dict[str, object]:
    lecture = Lecture.objects.get(id=lecture_id)
    if lecture_processing_stopped(lecture=lecture):
        return {
            "lecture_id": lecture_id,
            "chunk_count": 0,
            "status": lecture.transcription_status,
            "stopped": True,
        }
    timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
    if timeout_error is not None:
        return {
            "lecture_id": lecture_id,
            "chunk_count": 0,
            "status": Lecture.objects.get(id=lecture_id).transcription_status,
        }
    service = build_transcription_service()
    chunks = service.submission_candidates(lecture=lecture, start_index=start_index, limit=limit)
    with HeartbeatService.keepalive(lecture_id=lecture_id):
        for result in _submit_transcription_chunks(lecture_id=lecture_id, chunks=chunks):
            if result.get("status") == "FAILED":
                logger.warning(
                    "transcription_chunk_submit_failed",
                    extra={
                        "lecture_id": lecture_id,
                        "chunk_id": str(result.get("chunk_id", "")),
                        "error_message": str(result.get("reason", "")),
                    },
                )
            lecture.refresh_from_db()
            if lecture_transcription_failed(lecture=lecture):
                return {
                    "lecture_id": lecture_id,
                    "chunk_count": len(chunks),
                    "status": lecture.transcription_status,
                }

    lecture.refresh_from_db()
    if lecture_transcription_failed(lecture=lecture):
        lecture.refresh_from_db()
        return {
            "lecture_id": lecture_id,
            "chunk_count": len(chunks),
            "status": lecture.transcription_status,
        }
    if service.ready_for_merge(lecture=lecture):
        service.merge_transcript(lecture=lecture)
    lecture.refresh_from_db()
    if (
        lecture.transcription_status == TranscriptionStatus.COMPLETED
        and lecture.merged_transcript.strip()
    ):
        dispatch_note_generation_if_needed(lecture=lecture)
        lecture.refresh_from_db()
    return {
        "lecture_id": lecture_id,
        "chunk_count": len(chunks),
        "status": lecture.transcription_status,
    }


@shared_task(bind=True, ignore_result=True)
def cancel_timed_out_lectures(self) -> dict[str, object]:
    cancelled_ids: list[str] = []
    for lecture in Lecture.objects.filter(status="PROCESSING").iterator():
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            cancelled_ids.append(str(lecture.id))
            logger.warning(
                "lecture_processing_timed_out",
                extra={"lecture_id": str(lecture.id), "stage": timeout_error.stage},
            )
    return {"cancelled_count": len(cancelled_ids), "lecture_ids": cancelled_ids}


@shared_task(bind=True, ignore_result=True)
def recover_stalled_processing_jobs(self) -> dict[str, object]:
    """Phase 12: periodic scan for PROCESSING jobs whose heartbeat went stale.

    A no-op (returns immediately) unless PROCESSING_RECOVERY_ENABLED is set —
    heartbeat collection itself is unaffected either way. See
    apps.ai_pipeline.recovery.ProcessingRecoveryService for the scan and
    apps.ai_pipeline.jobs.ProcessingJobService.evaluate_and_recover_stale_job
    for the actual concurrency-safe lock/recheck/decide/commit transaction.
    """
    from apps.ai_pipeline.recovery import ProcessingRecoveryService

    result = ProcessingRecoveryService.run_scan()
    if result.get("recovered") or result.get("failed_permanently"):
        logger.warning("processing_recovery_scan_completed", extra=result)
    return result


@shared_task(bind=True, ignore_result=True)
def recover_stalled_transcriptions(self) -> dict[str, object]:
    if not settings.TRANSCRIPTION_RECOVERY_ENABLED:
        return {"recovered_count": 0, "lecture_ids": []}

    recovered_ids: list[str] = []
    service = build_transcription_service()
    for lecture in Lecture.objects.filter(
        transcription_status__in=[TranscriptionStatus.QUEUED, TranscriptionStatus.TRANSCRIBING]
    ).iterator():
        if lecture_processing_stopped(lecture=lecture):
            continue
        if lecture.audio_chunks.count() <= 0:
            continue
        pending_count = service.count_remaining_submission_candidates(lecture=lecture)
        if pending_count <= 0:
            continue
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            continue
        inactivity_seconds = transcription_inactivity_seconds(lecture=lecture)
        if (
            lecture.transcription_status == TranscriptionStatus.QUEUED
            and lecture.transcript_chunks.count() == 0
        ):
            logger.info(
                "transcription_recovery_enqueuing_queued_lecture",
                extra={
                    "lecture_id": str(lecture.id),
                    "missing_count": pending_count,
                },
            )
            ensure_transcription_started.run(str(lecture.id))
            recovered_ids.append(str(lecture.id))
            continue
        if (
            inactivity_seconds is None
            or inactivity_seconds < settings.TRANSCRIPTION_STUCK_TIMEOUT_SECONDS
        ):
            continue
        logger.info(
            "transcription_recovery_enqueuing_missing_chunks",
            extra={
                "lecture_id": str(lecture.id),
                "missing_count": pending_count,
                "inactivity_seconds": round(inactivity_seconds, 3),
            },
        )
        enqueue_transcription_batch(
            lecture_id=str(lecture.id),
            start_index=None,
            limit=settings.TRANSCRIPTION_BATCH_SIZE,
        )
        recovered_ids.append(str(lecture.id))
    return {"recovered_count": len(recovered_ids), "lecture_ids": recovered_ids}


@shared_task(bind=True, ignore_result=True)
def ensure_transcription_started(self, lecture_id: str) -> dict[str, object]:
    lecture = Lecture.objects.get(id=lecture_id)
    if lecture_processing_stopped(lecture=lecture):
        return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}
    if lecture.audio_chunks.count() <= 0:
        return {"lecture_id": lecture_id, "status": "NO_AUDIO_CHUNKS"}
    if lecture.transcription_status != TranscriptionStatus.QUEUED:
        return {"lecture_id": lecture_id, "status": lecture.transcription_status}
    if lecture.transcript_chunks.exists():
        return {"lecture_id": lecture_id, "status": "ALREADY_STARTED"}

    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="transcription-submit",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info(
                "transcription_startup_fallback_skipped_lock_busy", extra={"lecture_id": lecture_id}
            )
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}

        lecture.refresh_from_db()
        if lecture_processing_stopped(lecture=lecture):
            return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}
        if lecture.transcription_status != TranscriptionStatus.QUEUED:
            return {"lecture_id": lecture_id, "status": lecture.transcription_status}
        if lecture.transcript_chunks.exists():
            return {"lecture_id": lecture_id, "status": "ALREADY_STARTED"}

        logger.warning(
            "transcription_startup_fallback_running",
            extra={
                "lecture_id": lecture_id,
                "audio_chunk_count": lecture.audio_chunks.count(),
            },
        )
        return run_transcription_batch_sync(
            lecture_id=lecture_id,
            start_index=None,
            limit=None,
        )


def _dispatch_note_after_slides(*, lecture_id: str) -> None:
    lecture = Lecture.objects.get(id=lecture_id)
    if lecture_processing_stopped(lecture=lecture):
        return
    dispatch_note_generation_if_needed(lecture=lecture)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(PipelineError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def process_slides_pipeline(self, lecture_id: str) -> dict[str, object]:
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="slide-processing",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info("slide_processing_already_running", extra={"lecture_id": lecture_id})
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}

        lecture = Lecture.objects.get(id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}

        service = SlideProcessingService()
        service.update_progress(lecture=lecture, stage="PROCESSING", progress=5)

        try:
            with HeartbeatService.keepalive(lecture_id=lecture_id):
                debug_payload = service.run_pipeline(lecture=lecture)
        except PipelineError as exc:
            service.fail_pipeline(lecture=lecture, error=exc)
            logger.exception(
                "slide_processing_failed", extra={"lecture_id": lecture_id, "stage": exc.stage}
            )
            # A slide failure must not strand a note that was waiting on it.
            _dispatch_note_after_slides(lecture_id=lecture_id)
            return {
                "lecture_id": lecture_id,
                "status": "FAILED",
                "reason": exc.message,
                "stage": exc.stage,
            }

    logger.info("slide_processing_completed", extra={"lecture_id": lecture_id})
    # If the transcript finished first, the note was held back for the slides.
    _dispatch_note_after_slides(lecture_id=lecture_id)
    return debug_payload


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(PipelineError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
)
def process_lecture_pipeline(self, lecture_id: str, job_id: str = "") -> dict[str, object]:
    with StageConcurrencyStore.track_stage(queue="pipeline", task_id=self.request.id or lecture_id):
        return _process_lecture_pipeline_body(self, lecture_id, job_id)


def _process_lecture_pipeline_body(self, lecture_id: str, job_id: str = "") -> dict[str, object]:
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="lecture-pipeline",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info("lecture_processing_already_running", extra={"lecture_id": lecture_id})
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}

        orchestrator = LectureProcessingOrchestrator()
        lecture = Lecture.objects.get(id=lecture_id)
        expected_generation: int | None = None
        if job_id:
            started_job = ProcessingJobService.mark_started(
                job_id=job_id, task_id=self.request.id or ""
            )
            if started_job is not None:
                expected_generation = started_job.execution_generation
            if (
                started_job is not None
                and started_job.scheduler_managed
                and getattr(started_job, "_scheduler_started_now", False)
            ):
                # The scheduler leaves the lecture truthful while it waits for a
                # global slot. Only a worker transitions it into processing.
                orchestrator.start(
                    lecture=lecture, task_id=self.request.id or started_job.celery_task_id
                )
                lecture.refresh_from_db()
        HeartbeatService.tick(lecture_id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            logger.warning(
                "lecture_processing_timed_out",
                extra={"lecture_id": lecture_id, "stage": timeout_error.stage},
            )
            return {"lecture_id": lecture_id, "status": "FAILED", "reason": timeout_error.message}
        try:
            with measure_scaling_stage(
                stage="lecture_pipeline",
                lecture_id=lecture_id,
                task_id=self.request.id,
                task_name=self.name,
            ):
                payload = orchestrator.run(lecture=lecture)
        except PipelineError as exc:
            orchestrator.fail(lecture=lecture, error=exc, expected_generation=expected_generation)
            if exc.stage in {"PROCESSING_TIMEOUT", "TRANSCRIPTION_TIMEOUT"}:
                logger.warning(
                    "lecture_processing_timed_out",
                    extra={"lecture_id": lecture_id, "stage": exc.stage},
                )
                return {"lecture_id": lecture_id, "status": "FAILED", "reason": exc.message}
            logger.exception(
                "lecture_processing_failed", extra={"lecture_id": lecture_id, "stage": exc.stage}
            )
            return {
                "lecture_id": lecture_id,
                "status": "FAILED",
                "reason": exc.message,
                "stage": exc.stage,
            }
        except Exception as exc:
            if isinstance(exc, StorageProviderError):
                error = PipelineError(
                    stage="STORAGE",
                    message="ذخیره‌سازی فایل موقتاً با مشکل روبه‌رو شد. لطفاً چند دقیقه دیگر دوباره تلاش کنید.",
                )
            else:
                error = PipelineError(
                    stage="SYSTEM",
                    message="پردازش فایل با خطای غیرمنتظره روبه‌رو شد. لطفاً دوباره تلاش کنید.",
                )
            orchestrator.fail(lecture=lecture, error=error, expected_generation=expected_generation)
            logger.exception(
                "lecture_processing_failed",
                extra={
                    "lecture_id": lecture_id,
                    "stage": error.stage,
                    "error_type": exc.__class__.__name__,
                    "error_detail": str(exc.__cause__ or exc)[:500],
                },
            )
            return {
                "lecture_id": lecture_id,
                "status": "FAILED",
                "reason": error.message,
                "stage": error.stage,
            }

    logger.info("lecture_processing_completed", extra={"lecture_id": lecture_id})
    return payload


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(PipelineError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    ignore_result=True,
)
def transcribe_chunks_batch(
    self,
    lecture_id: str,
    start_index: int | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    with StageConcurrencyStore.track_stage(
        queue="transcription", task_id=self.request.id or lecture_id
    ):
        return _transcribe_chunks_batch_body(self, lecture_id, start_index, limit)


def _transcribe_chunks_batch_body(
    self,
    lecture_id: str,
    start_index: int | None = None,
    limit: int | None = None,
) -> dict[str, object]:
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="transcription-submit",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            lecture = Lecture.objects.get(id=lecture_id)
            if lecture_processing_stopped(lecture=lecture):
                return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}
            logger.info(
                "transcription_submission_already_running", extra={"lecture_id": lecture_id}
            )
            enqueue_transcription_batch(
                lecture_id=lecture_id, start_index=start_index, limit=limit, countdown=2
            )
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            with measure_scaling_stage(
                stage="transcription_batch",
                lecture_id=lecture_id,
                task_id=self.request.id,
                task_name=self.name,
            ):
                return run_transcription_batch_sync(
                    lecture_id=lecture_id, start_index=start_index, limit=limit
                )

        lecture = Lecture.objects.get(id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            logger.warning(
                "transcription_submission_timed_out",
                extra={"lecture_id": lecture_id, "stage": timeout_error.stage},
            )
            return {"lecture_id": lecture_id, "status": "FAILED", "reason": timeout_error.message}

        service = build_transcription_service()
        batch_limit = limit or settings.TRANSCRIPTION_BATCH_SIZE
        chunks = service.submission_candidates(
            lecture=lecture,
            start_index=start_index,
            limit=batch_limit,
        )
        if lecture_transcription_failed(lecture=lecture):
            lecture.refresh_from_db()
            return {
                "lecture_id": lecture_id,
                "chunk_count": 0,
                "status": lecture.transcription_status,
            }
        if not chunks:
            if service.ready_for_merge(lecture=lecture):
                service.merge_transcript(lecture=lecture)
                lecture.refresh_from_db()
                if (
                    lecture.transcription_status == TranscriptionStatus.COMPLETED
                    and lecture.merged_transcript.strip()
                ):
                    dispatch_note_generation_if_needed(lecture=lecture)
                return {
                    "lecture_id": lecture_id,
                    "chunk_count": 0,
                    "status": lecture.transcription_status,
                }
            pending_submission_chunks = service.pending_submission_placeholders(lecture=lecture)
            pending_chunks = service.pending_poll_chunks(lecture=lecture)
            recoverable_submission_chunks = [
                chunk
                for chunk in pending_submission_chunks
                if service._submission_needs_recovery(transcript_chunk=chunk)
                or bool((chunk.processing_metadata or {}).get("retryable"))
            ]
            if recoverable_submission_chunks:
                enqueue_transcription_batch(
                    lecture_id=lecture_id, start_index=None, limit=batch_limit, countdown=1
                )
            if not pending_chunks and not service._audio_chunk_generation_complete(lecture):
                enqueue_transcription_batch(
                    lecture_id=lecture_id, start_index=start_index, limit=batch_limit, countdown=2
                )
            if pending_chunks or pending_submission_chunks:
                enqueue_transcription_poll(lecture_id=lecture_id, attempt=0, countdown=2)
            return {
                "lecture_id": lecture_id,
                "chunk_count": 0,
                "status": lecture.transcription_status,
            }

        with (
            measure_scaling_stage(
                stage="transcription_batch",
                lecture_id=lecture_id,
                task_id=self.request.id,
                task_name=self.name,
                chunk_count=len(chunks),
            ),
            HeartbeatService.keepalive(lecture_id=lecture_id),
        ):
            for result in _submit_transcription_chunks(lecture_id=lecture_id, chunks=chunks):
                if result.get("status") == "FAILED":
                    logger.warning(
                        "transcription_chunk_submit_failed",
                        extra={
                            "lecture_id": lecture_id,
                            "chunk_id": str(result.get("chunk_id", "")),
                            "error_message": str(result.get("reason", "")),
                        },
                    )
                lecture.refresh_from_db()
                if lecture_transcription_failed(lecture=lecture):
                    return {
                        "lecture_id": lecture_id,
                        "chunk_count": len(chunks),
                        "status": lecture.transcription_status,
                    }

        lecture.refresh_from_db()
        if lecture_transcription_failed(lecture=lecture):
            lecture.refresh_from_db()
            return {
                "lecture_id": lecture_id,
                "chunk_count": len(chunks),
                "status": lecture.transcription_status,
            }
        if service.ready_for_merge(lecture=lecture):
            service.merge_transcript(lecture=lecture)
            lecture.refresh_from_db()
            if (
                lecture.transcription_status == TranscriptionStatus.COMPLETED
                and lecture.merged_transcript.strip()
            ):
                dispatch_note_generation_if_needed(lecture=lecture)
            return {
                "lecture_id": lecture_id,
                "chunk_count": len(chunks),
                "status": lecture.transcription_status,
            }

        remaining_candidates = service.count_remaining_submission_candidates(
            lecture=lecture,
            start_index=(chunks[-1].chunk_index + 1) if chunks else start_index,
        )
        total_remaining_candidates = service.count_remaining_submission_candidates(lecture=lecture)
        if remaining_candidates > 0:
            logger.info(
                "transcription_next_batch_enqueued",
                extra={
                    "lecture_id": lecture_id,
                    "next_start_index": chunks[-1].chunk_index + 1,
                    "remaining_count": remaining_candidates,
                },
            )
            enqueue_transcription_batch(
                lecture_id=lecture_id,
                start_index=chunks[-1].chunk_index + 1,
                limit=batch_limit,
                countdown=1,
            )

        if total_remaining_candidates > remaining_candidates:
            enqueue_transcription_batch(
                lecture_id=lecture_id, start_index=None, limit=batch_limit, countdown=10
            )

        enqueue_transcription_poll(lecture_id=lecture_id, attempt=0, countdown=2)

        logger.info(
            "transcription_batch_submitted",
            extra={
                "lecture_id": lecture_id,
                "submitted_count": len(chunks),
                "transcription_status": lecture.transcription_status,
            },
        )
        return {
            "lecture_id": lecture_id,
            "chunk_count": len(chunks),
            "status": lecture.transcription_status,
        }


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(PipelineError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    ignore_result=True,
)
def poll_transcription_chunks_batch(self, lecture_id: str, attempt: int = 0) -> dict[str, object]:
    with StageConcurrencyStore.track_stage(
        queue="transcription", task_id=self.request.id or lecture_id
    ):
        return _poll_transcription_chunks_batch_body(self, lecture_id, attempt)


def _poll_transcription_chunks_batch_body(
    self, lecture_id: str, attempt: int = 0
) -> dict[str, object]:
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="transcription-poll",
        ttl_seconds=max(120, settings.LECTURE_TASK_LOCK_TTL_SECONDS // 2),
    ) as acquired:
        if not acquired:
            lecture = Lecture.objects.get(id=lecture_id)
            if lecture_processing_stopped(lecture=lecture):
                return {
                    "lecture_id": lecture_id,
                    "status": lecture.status,
                    "attempt": attempt,
                    "stopped": True,
                }
            logger.info("transcription_poll_already_running", extra={"lecture_id": lecture_id})
            enqueue_transcription_poll(lecture_id=lecture_id, attempt=attempt, countdown=2)
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING", "attempt": attempt}

        lecture = Lecture.objects.get(id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return {
                "lecture_id": lecture_id,
                "status": lecture.status,
                "attempt": attempt,
                "stopped": True,
            }
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            logger.warning(
                "transcription_poll_timed_out",
                extra={"lecture_id": lecture_id, "stage": timeout_error.stage},
            )
            return {
                "lecture_id": lecture_id,
                "status": "FAILED",
                "attempt": attempt,
                "reason": timeout_error.message,
            }
        service = build_transcription_service()
        pending_submission_chunks = service.pending_submission_placeholders(lecture=lecture)
        recoverable_submission_chunks = [
            chunk
            for chunk in pending_submission_chunks
            if service._submission_needs_recovery(transcript_chunk=chunk)
            or bool((chunk.processing_metadata or {}).get("retryable"))
        ]
        if lecture_transcription_failed(lecture=lecture):
            lecture.refresh_from_db()
            return {
                "lecture_id": lecture_id,
                "status": lecture.transcription_status,
                "attempt": attempt,
            }
        if recoverable_submission_chunks:
            logger.info(
                "transcription_submission_placeholders_requeued",
                extra={
                    "lecture_id": lecture_id,
                    "placeholder_count": len(recoverable_submission_chunks),
                },
            )
            enqueue_transcription_batch(
                lecture_id=lecture_id,
                start_index=None,
                limit=settings.TRANSCRIPTION_BATCH_SIZE,
                countdown=1,
            )
        transcript_chunks = service.pending_poll_chunks(lecture=lecture)
        if not transcript_chunks:
            if service.ready_for_merge(lecture=lecture):
                service.merge_transcript(lecture=lecture)
                lecture.refresh_from_db()
                if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
                    run_note_generation_sync(lecture_id=lecture_id)
                elif (
                    lecture.transcription_status == TranscriptionStatus.COMPLETED
                    and lecture.merged_transcript.strip()
                ):
                    dispatch_note_generation_if_needed(lecture=lecture)
            else:
                if pending_submission_chunks:
                    enqueue_transcription_poll(
                        lecture_id=lecture_id, attempt=attempt + 1, countdown=2
                    )
                    return {
                        "lecture_id": lecture_id,
                        "status": "PENDING_SUBMISSION",
                        "attempt": attempt,
                    }
                missing_count = service.count_remaining_submission_candidates(lecture=lecture)
                if missing_count > 0:
                    enqueue_transcription_batch(
                        lecture_id=lecture_id,
                        start_index=None,
                        limit=settings.TRANSCRIPTION_BATCH_SIZE,
                        countdown=1,
                    )
            return {
                "lecture_id": lecture_id,
                "status": lecture.transcription_status,
                "attempt": attempt,
            }

        has_pending = False
        failed_chunks: list[int] = []
        for transcript_chunk in transcript_chunks:
            lecture.refresh_from_db()
            timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
            if timeout_error is not None:
                logger.warning(
                    "transcription_poll_timed_out",
                    extra={"lecture_id": lecture_id, "stage": timeout_error.stage},
                )
                return {
                    "lecture_id": lecture_id,
                    "status": "FAILED",
                    "attempt": attempt,
                    "reason": timeout_error.message,
                }
            completed, failed = service.poll_chunk(transcript_chunk=transcript_chunk)
            lecture.refresh_from_db()
            if lecture_transcription_failed(lecture=lecture):
                return {
                    "lecture_id": lecture_id,
                    "status": lecture.transcription_status,
                    "attempt": attempt,
                }
            if failed:
                failed_chunks.append(transcript_chunk.chunk_index)
            elif not completed:
                has_pending = True

        if failed_chunks:
            logger.warning(
                "transcription_chunks_failed_waiting_for_recovery",
                extra={"lecture_id": lecture_id, "failed_chunks": failed_chunks},
            )
            enqueue_transcription_batch(
                lecture_id=lecture_id,
                start_index=None,
                limit=settings.TRANSCRIPTION_BATCH_SIZE,
                countdown=1,
            )

        if has_pending:
            missing_count = service.count_remaining_submission_candidates(lecture=lecture)
            if missing_count > 0:
                logger.info(
                    "transcription_missing_chunks_detected_while_pending",
                    extra={"lecture_id": lecture_id, "missing_count": missing_count},
                )
                enqueue_transcription_batch(
                    lecture_id=lecture_id,
                    start_index=None,
                    limit=settings.TRANSCRIPTION_BATCH_SIZE,
                    countdown=1,
                )
            if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
                if attempt >= 20:
                    error = PipelineError(
                        stage="TRANSCRIBING",
                        message="Avanegar polling timed out before chunks completed.",
                    )
                    service.fail(lecture=lecture, error=error)
                    raise error
                time.sleep(2)
                return poll_transcription_chunks_batch.run(
                    lecture_id=lecture_id, attempt=attempt + 1
                )
            enqueue_transcription_poll(lecture_id=lecture_id, attempt=attempt + 1, countdown=3)
            return {"lecture_id": lecture_id, "status": "PENDING", "attempt": attempt}

        lecture.refresh_from_db()
        if lecture_transcription_failed(lecture=lecture):
            lecture.refresh_from_db()
            return {
                "lecture_id": lecture_id,
                "status": lecture.transcription_status,
                "attempt": attempt,
            }
        if service.ready_for_merge(lecture=lecture):
            service.merge_transcript(lecture=lecture)
            lecture.refresh_from_db()
            if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
                run_note_generation_sync(lecture_id=lecture_id)
            else:
                dispatch_note_generation_if_needed(lecture=lecture)
        else:
            missing_count = service.count_remaining_submission_candidates(lecture=lecture)
            if missing_count > 0:
                logger.info(
                    "transcription_missing_chunks_detected_after_poll",
                    extra={"lecture_id": lecture_id, "missing_count": missing_count},
                )
                enqueue_transcription_batch(
                    lecture_id=lecture_id,
                    start_index=None,
                    limit=settings.TRANSCRIPTION_BATCH_SIZE,
                    countdown=1,
                )
        return {
            "lecture_id": lecture_id,
            "status": lecture.transcription_status,
            "attempt": attempt,
        }
