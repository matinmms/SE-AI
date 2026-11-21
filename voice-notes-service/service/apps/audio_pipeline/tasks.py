from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

from apps.ai_pipeline.exceptions import PipelineError
from apps.ai_pipeline.tasks import lecture_processing_stopped
from apps.audio_pipeline.providers.router_provider import RoutedTranscriptionProvider
from apps.audio_pipeline.services import AudioPreprocessingService
from apps.audio_pipeline.transcription.service import TranscriptionOrchestratorService
from apps.lectures.models import Lecture
from apps.audio_pipeline.models import AudioChunk
from infrastructure.queue.control import lecture_task_lock

logger = logging.getLogger("celery")


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(PipelineError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    ignore_result=True,
)
def process_audio_pipeline(self, lecture_id: str) -> dict[str, object]:
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="audio-processing",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info("audio_processing_already_running", extra={"lecture_id": lecture_id})
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}

        lecture = Lecture.objects.get(id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}

        service = AudioPreprocessingService()
        try:
            payload = service.run_pipeline(lecture=lecture)
        except PipelineError as exc:
            service.fail_pipeline(lecture=lecture, error=exc)
            logger.exception("audio_processing_failed", extra={"lecture_id": lecture_id, "stage": exc.stage})
            return {"lecture_id": lecture_id, "status": "FAILED", "reason": exc.message, "stage": exc.stage}

    logger.info("audio_processing_completed", extra={"lecture_id": lecture_id})
    return payload


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    dont_autoretry_for=(PipelineError,),
    retry_backoff=True,
    retry_kwargs={"max_retries": 3},
    ignore_result=True,
)
def transcribe_chunk(self, chunk_id: str) -> dict[str, object]:
    from apps.ai_costs.context import avalai_cost_context
    from apps.ai_pipeline.models import LectureProcessingJob

    chunk = AudioChunk.objects.select_related("lecture").get(id=chunk_id)
    if lecture_processing_stopped(lecture=chunk.lecture):
        return {"chunk_id": chunk_id, "lecture_id": str(chunk.lecture_id), "status": chunk.lecture.status, "stopped": True}
    service = TranscriptionOrchestratorService(provider=RoutedTranscriptionProvider())
    job = (
        LectureProcessingJob.objects.filter(lecture_id=chunk.lecture_id)
        .order_by("-created_at")
        .first()
    )
    try:
        with avalai_cost_context(
            user_id=str(chunk.lecture.user_id),
            lecture_id=str(chunk.lecture_id),
            processing_job_id=str(job.id) if job else "",
            feature="transcription",
        ):
            transcript_chunk = service.transcribe_chunk(chunk=chunk)
    except PipelineError as exc:
        service.fail(lecture=chunk.lecture, error=exc)
        logger.exception("transcription_chunk_failed", extra={"chunk_id": chunk_id, "lecture_id": str(chunk.lecture_id), "stage": exc.stage})
        return {"chunk_id": chunk_id, "lecture_id": str(chunk.lecture_id), "status": "FAILED", "reason": exc.message, "stage": exc.stage}

    logger.info("transcription_chunk_completed", extra={"chunk_id": chunk_id, "lecture_id": str(chunk.lecture_id)})
    return {
        "chunk_id": str(transcript_chunk.id),
        "lecture_id": str(chunk.lecture_id),
        "provider_job_id": transcript_chunk.provider_job_id,
        "status": chunk.lecture.transcription_status,
    }
