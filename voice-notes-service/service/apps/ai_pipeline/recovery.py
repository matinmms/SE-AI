from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from apps.ai_pipeline.models import LectureProcessingJob, LectureProcessingJobStatus
from apps.lectures.models import (
    AudioProcessingStatus,
    Lecture,
    SlideProcessingStatus,
    TranscriptionStatus,
)
from infrastructure.observability.scaling import measure_scaling_stage

logger = logging.getLogger("tesanote.processing_recovery")

SAFE_REPLAYABLE = "SAFE_REPLAYABLE"
AMBIGUOUS = "AMBIGUOUS"

# Failure class boundary (see Phase 12 audit): Phase 8's own stale-claim
# recovery already owns QUEUED jobs that were claimed but never started
# (`started_at IS NULL`) — see FairSchedulerService._recover_stale_claims_locked.
# This module targets only jobs that already reached PROCESSING and then went
# quiet; the two candidate sets are disjoint by construction.


def classify_stage(*, lecture: Lecture) -> str:
    """Conservative, provider-cost-aware classification of a stale job.

    SAFE_REPLAYABLE: nothing provider-billed and no partially-produced local
    artifacts exist yet — re-running the pipeline from the top is equivalent
    to the very first attempt. This is deliberately a narrow window: once
    audio preprocessing has actually started producing chunks, or
    transcription/notes have begun, the pipeline has no per-stage checkpoint
    to resume from, and a fresh run could duplicate provider-billed work
    (transcription, note generation) or collide with already-created rows
    (unique_audio_chunk_per_lecture and friends). AMBIGUOUS covers all of
    that — the conservative, non-recoverable-automatically outcome.
    """
    if lecture.audio_processing_status not in {
        AudioProcessingStatus.NOT_STARTED,
        AudioProcessingStatus.QUEUED,
    }:
        return AMBIGUOUS
    if lecture.audio_chunks.exists():
        return AMBIGUOUS
    if lecture.transcription_status != TranscriptionStatus.NOT_STARTED:
        return AMBIGUOUS
    if lecture.slide_processing_status not in {
        SlideProcessingStatus.NOT_STARTED,
        SlideProcessingStatus.QUEUED,
    }:
        return AMBIGUOUS
    from apps.notes.generation import aggregate_note_status
    from apps.notes.models import NoteGenerationStatus

    note_status, _ = aggregate_note_status(lecture=lecture)
    if note_status != NoteGenerationStatus.NOT_STARTED:
        return AMBIGUOUS
    return SAFE_REPLAYABLE


class ProcessingRecoveryService:
    """Phase 12: finds and evaluates PROCESSING jobs whose heartbeat went stale.

    The actual lock/recheck/decide/mutate transaction lives on
    ProcessingJobService.evaluate_and_recover_stale_job — this service only
    owns the (cheap, indexed, unlocked) candidate scan and admin/metrics
    surfacing, so a batch of candidates can be discovered without holding any
    row lock while iterating.
    """

    @staticmethod
    def stale_cutoff():
        return timezone.now() - timedelta(seconds=settings.PROCESSING_HEARTBEAT_STALE_SECONDS)

    @classmethod
    def run_scan(cls, *, batch_size: int | None = None) -> dict[str, int]:
        if not settings.PROCESSING_RECOVERY_ENABLED:
            return {
                "enabled": 0,
                "candidates": 0,
                "recovered": 0,
                "failed_permanently": 0,
                "skipped": 0,
            }

        from apps.ai_pipeline.jobs import ProcessingJobService

        limit = batch_size or settings.PROCESSING_RECOVERY_SCAN_BATCH_SIZE
        cutoff = cls.stale_cutoff()
        candidate_ids = list(
            LectureProcessingJob.objects.filter(status=LectureProcessingJobStatus.PROCESSING)
            .filter(_stale_heartbeat_q(cutoff=cutoff))
            .order_by("started_at")
            .values_list("id", flat=True)[:limit]
        )

        outcomes = {"recovered": 0, "failed_permanently": 0, "skipped": 0}
        for job_id in candidate_ids:
            outcome = ProcessingJobService.evaluate_and_recover_stale_job(job_id=str(job_id))
            if outcome in outcomes:
                outcomes[outcome] += 1
            else:
                outcomes["skipped"] += 1

        if outcomes["recovered"] or outcomes["failed_permanently"]:
            with measure_scaling_stage(
                stage="processing_recovery_scan",
                candidates=len(candidate_ids),
                **outcomes,
            ):
                pass

        return {"enabled": 1, "candidates": len(candidate_ids), **outcomes}

    @classmethod
    def admin_summary(cls) -> dict[str, object]:
        cutoff = cls.stale_cutoff()
        stale_candidate_count = LectureProcessingJob.objects.filter(
            status=LectureProcessingJobStatus.PROCESSING
        ).filter(_stale_heartbeat_q(cutoff=cutoff)).count()
        today = timezone.localdate()
        return {
            "heartbeat_enabled": bool(settings.PROCESSING_HEARTBEAT_ENABLED),
            "recovery_enabled": bool(settings.PROCESSING_RECOVERY_ENABLED),
            "stale_threshold_seconds": settings.PROCESSING_HEARTBEAT_STALE_SECONDS,
            "max_recovery_attempts": settings.PROCESSING_MAX_RECOVERY_ATTEMPTS,
            "stale_candidate_count": stale_candidate_count,
            "recovered_today": LectureProcessingJob.objects.filter(
                last_recovery_at__date=today,
            ).exclude(last_recovery_reason="").count(),
        }


def _stale_heartbeat_q(*, cutoff):
    return Q(last_heartbeat_at__isnull=True) | Q(last_heartbeat_at__lt=cutoff)
