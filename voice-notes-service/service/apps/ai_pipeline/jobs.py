from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from apps.ai_pipeline.models import (
    LectureProcessingFailureCategory,
    LectureProcessingJob,
    LectureProcessingJobStatus,
)
from apps.lectures.models import Lecture, LectureStatus
from apps.usage.models import UsageLog
from infrastructure.observability.scaling import measure_scaling_stage

ACTIVE_JOB_STATUSES = (LectureProcessingJobStatus.QUEUED, LectureProcessingJobStatus.PROCESSING)
PENDING_JOB_STATUS = LectureProcessingJobStatus.PENDING

logger = logging.getLogger("tesanote")


class ProcessingJobState:
    STARTED = "STARTED"
    ACTIVE_EXISTS = "ACTIVE_EXISTS"
    PENDING = "PENDING"
    CAPACITY_FULL = "CAPACITY_FULL"
    EXISTING_ACTIVE = "EXISTING_ACTIVE"


@dataclass(frozen=True)
class ProcessingJobDecision:
    job: LectureProcessingJob
    created: bool
    processing_state: str


class ProcessingJobService:
    """Database-backed lifecycle operations for a lecture processing attempt."""

    @staticmethod
    def _idempotency_key(*, lecture_id: str, attempt: int) -> str:
        payload = f"lecture-processing:{lecture_id}:attempt:{attempt}".encode()
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _failure_category(*, stage: str) -> str:
        normalized = stage.upper()
        if "DISPATCH" in normalized:
            return LectureProcessingFailureCategory.DISPATCH_FAILED
        if "TRANSCRIB" in normalized:
            return LectureProcessingFailureCategory.TRANSCRIPTION_FAILED
        if "PDF" in normalized:
            return LectureProcessingFailureCategory.PDF_FAILED
        if "NOTE" in normalized or "GENERATION" in normalized:
            return LectureProcessingFailureCategory.GENERATION_FAILED
        return LectureProcessingFailureCategory.INTERNAL_ERROR

    @classmethod
    @transaction.atomic
    def queue_or_reuse(
        cls,
        *,
        lecture_id: str,
        user_id: str,
        celery_task_id: str,
    ) -> ProcessingJobDecision:
        """Backward-compatible entry point for the Phase 2 guarded start path."""
        return cls.start_or_queue_after_current(
            lecture_id=lecture_id,
            user_id=user_id,
            celery_task_id=celery_task_id,
            confirm_pending=False,
        )

    @classmethod
    @transaction.atomic
    def start_or_queue_after_current(
        cls,
        *,
        lecture_id: str,
        user_id: str,
        celery_task_id: str,
        confirm_pending: bool,
    ) -> ProcessingJobDecision:
        """Enforce one active and one pending processing job for each user.

        The user row lock is intentionally the serialization point: jobs belong to
        different lecture rows, so a lecture-only lock cannot prevent two tabs from
        accepting two active lectures for the same account.
        """
        get_user_model().objects.select_for_update().get(id=user_id)
        lecture = Lecture.objects.select_for_update().get(id=lecture_id, user_id=user_id)
        existing = (
            LectureProcessingJob.objects.select_for_update()
            .filter(lecture=lecture)
            .exclude(
                status__in=(LectureProcessingJobStatus.SUCCEEDED, LectureProcessingJobStatus.FAILED)
            )
            .order_by("-attempt")
            .first()
        )
        if existing is not None:
            state = (
                ProcessingJobState.PENDING
                if existing.status == PENDING_JOB_STATUS
                else ProcessingJobState.EXISTING_ACTIVE
            )
            return ProcessingJobDecision(job=existing, created=False, processing_state=state)

        active = (
            LectureProcessingJob.objects.select_for_update()
            .filter(user_id=user_id, status__in=ACTIVE_JOB_STATUSES)
            .order_by("queued_at", "created_at")
            .first()
        )
        pending = (
            LectureProcessingJob.objects.select_for_update()
            .filter(user_id=user_id, status=PENDING_JOB_STATUS)
            .order_by("created_at")
            .first()
        )
        if active is not None:
            if pending is not None:
                cls._emit_capacity_rejected(job=active)
                return ProcessingJobDecision(
                    job=pending,
                    created=False,
                    processing_state=ProcessingJobState.CAPACITY_FULL,
                )
            if not confirm_pending:
                return ProcessingJobDecision(
                    job=active,
                    created=False,
                    processing_state=ProcessingJobState.ACTIVE_EXISTS,
                )
            job = cls._create_job(
                lecture=lecture,
                user_id=user_id,
                celery_task_id="",
                status=PENDING_JOB_STATUS,
                stage="PENDING",
            )
            cls._emit_pending_created(job=job)
            return ProcessingJobDecision(
                job=job, created=True, processing_state=ProcessingJobState.PENDING
            )

        # A terminal callback will promote a pending record immediately after its
        # transaction commits. During that short handoff window we preserve the
        # existing pending item rather than accepting a second one.
        if pending is not None:
            cls._emit_capacity_rejected(job=pending)
            return ProcessingJobDecision(
                job=pending,
                created=False,
                processing_state=ProcessingJobState.CAPACITY_FULL,
            )

        job = cls._create_job(
            lecture=lecture,
            user_id=user_id,
            celery_task_id=celery_task_id,
            status=LectureProcessingJobStatus.QUEUED,
            stage="QUEUED",
        )
        return ProcessingJobDecision(
            job=job, created=True, processing_state=ProcessingJobState.STARTED
        )

    @classmethod
    def _create_job(
        cls,
        *,
        lecture: Lecture,
        user_id: str,
        celery_task_id: str,
        status: str,
        stage: str,
    ) -> LectureProcessingJob:
        attempt_query = LectureProcessingJob.objects.filter(lecture=lecture).aggregate(
            max_attempt=Max("attempt")
        )
        attempt = int(attempt_query["max_attempt"] or 0) + 1
        usage_log = (
            UsageLog.objects.select_for_update()
            .filter(lecture=lecture)
            .order_by("-created_at")
            .first()
        )
        now = timezone.now()
        scheduler_managed = bool(getattr(settings, "FAIR_SCHEDULER_ENABLED", False))
        return LectureProcessingJob.objects.create(
            user_id=user_id,
            lecture=lecture,
            usage_log=usage_log,
            attempt=attempt,
            idempotency_key=cls._idempotency_key(lecture_id=str(lecture.id), attempt=attempt),
            status=status,
            stage="SCHEDULER_WAIT"
            if scheduler_managed and status == LectureProcessingJobStatus.QUEUED
            else stage,
            progress=0,
            celery_task_id=celery_task_id,
            queued_at=now,
            scheduler_managed=scheduler_managed,
            scheduler_ready_at=now if status == LectureProcessingJobStatus.QUEUED else None,
        )

    @staticmethod
    def _emit_pending_created(*, job: LectureProcessingJob) -> None:
        with measure_scaling_stage(
            stage="processing_pending_created",
            lecture_id=str(job.lecture_id),
            job_id=str(job.id),
        ):
            pass

    @staticmethod
    def _emit_capacity_rejected(*, job: LectureProcessingJob) -> None:
        with measure_scaling_stage(
            stage="processing_pending_rejected_capacity",
            lecture_id=str(job.lecture_id),
            job_id=str(job.id),
        ):
            pass

    @staticmethod
    def active_for_lecture(*, lecture_id: str) -> LectureProcessingJob | None:
        return (
            LectureProcessingJob.objects.filter(
                lecture_id=lecture_id,
                status__in=ACTIVE_JOB_STATUSES,
            )
            .order_by("-attempt")
            .first()
        )

    @classmethod
    def _schedule_pending_promotion(cls, *, user_id: str) -> None:
        def _promote_and_kick() -> None:
            cls.promote_next_pending_for_user(user_id=user_id)
            from apps.ai_pipeline.scheduler import FairSchedulerService

            FairSchedulerService.kick()

        transaction.on_commit(_promote_and_kick)

    @staticmethod
    def _schedule_temporary_artifact_expiration(*, lecture_id: str, finished_at) -> None:
        """Keep sources available for a full retry window after terminal processing."""
        from apps.storage_lifecycle.services import StorageArtifactLifecycleService

        transaction.on_commit(
            lambda: StorageArtifactLifecycleService.schedule_temporary_expiration_for_lecture(
                lecture_id=lecture_id,
                finished_at=finished_at,
            )
        )

    @classmethod
    def promote_next_pending_for_user(cls, *, user_id: str) -> LectureProcessingJob | None:
        """Promote the oldest durable pending job once a user's active slot is free."""
        promoted: LectureProcessingJob | None = None
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(id=user_id)
            if LectureProcessingJob.objects.filter(
                user_id=user_id, status__in=ACTIVE_JOB_STATUSES
            ).exists():
                return None
            job = (
                LectureProcessingJob.objects.select_for_update()
                .select_related("lecture")
                .filter(user_id=user_id, status=PENDING_JOB_STATUS)
                .order_by("created_at")
                .first()
            )
            if job is None:
                return None

            now = timezone.now()
            task_id = str(uuid4())
            pending_wait_ms = max(0, round((now - job.created_at).total_seconds() * 1000))
            scheduler_managed = bool(getattr(settings, "FAIR_SCHEDULER_ENABLED", False))
            job.status = LectureProcessingJobStatus.QUEUED
            job.stage = "SCHEDULER_WAIT" if scheduler_managed else "QUEUED"
            job.celery_task_id = task_id
            job.queued_at = now
            job.scheduler_managed = scheduler_managed
            job.scheduler_ready_at = now
            job.scheduler_claimed_at = None
            job.scheduler_dispatched_at = None
            job.scheduler_last_dispatch_error = ""
            job.save(
                update_fields=[
                    "status",
                    "stage",
                    "celery_task_id",
                    "queued_at",
                    "scheduler_managed",
                    "scheduler_ready_at",
                    "scheduler_claimed_at",
                    "scheduler_dispatched_at",
                    "scheduler_last_dispatch_error",
                    "updated_at",
                ]
            )

            if not scheduler_managed:
                # Legacy dispatch preserves the existing behavior while the
                # fair scheduler is disabled for a rollback or safe rollout.
                from apps.ai_pipeline.services import LectureProcessingOrchestrator

                LectureProcessingOrchestrator().start(lecture=job.lecture, task_id=task_id)
            transaction.on_commit(
                lambda: cls.dispatch_queued_processing(
                    job_id=str(job.id), lecture_id=str(job.lecture_id), task_id=task_id
                )
            )
            promoted = job

        with measure_scaling_stage(
            stage="processing_pending_wait",
            lecture_id=str(promoted.lecture_id),
            task_id=promoted.celery_task_id,
            job_id=str(promoted.id),
            pending_wait_ms=pending_wait_ms,
        ):
            pass
        with measure_scaling_stage(
            stage="processing_pending_promoted",
            lecture_id=str(promoted.lecture_id),
            task_id=promoted.celery_task_id,
            job_id=str(promoted.id),
        ):
            pass
        return promoted

    @classmethod
    def dispatch_queued_processing(cls, *, job_id: str, lecture_id: str, task_id: str) -> None:
        """Route committed work through the fair scheduler or legacy fallback."""
        job = LectureProcessingJob.objects.filter(id=job_id).select_related("lecture").first()
        if job is None or job.status != LectureProcessingJobStatus.QUEUED:
            return
        if job.scheduler_managed and bool(getattr(settings, "FAIR_SCHEDULER_ENABLED", False)):
            from apps.ai_pipeline.scheduler import FairSchedulerService

            FairSchedulerService.kick()
            return

        scheduler_rollback_start = False
        if job.scheduler_managed:
            # Turning the flag off is an operational rollback, not a data
            # repair. Drain durable ready work through the proven legacy path.
            with transaction.atomic():
                job = (
                    LectureProcessingJob.objects.select_for_update()
                    .select_related("lecture")
                    .get(id=job_id)
                )
                if job.status != LectureProcessingJobStatus.QUEUED:
                    return
                scheduler_rollback_start = job.lecture.status != LectureStatus.PROCESSING
                job.scheduler_managed = False
                job.scheduler_claimed_at = None
                job.scheduler_dispatched_at = timezone.now()
                job.stage = "QUEUED"
                job.save(
                    update_fields=[
                        "scheduler_managed",
                        "scheduler_claimed_at",
                        "scheduler_dispatched_at",
                        "stage",
                        "updated_at",
                    ]
                )
        elif job.scheduler_dispatched_at is None:
            LectureProcessingJob.objects.filter(
                id=job.id, scheduler_dispatched_at__isnull=True
            ).update(scheduler_dispatched_at=timezone.now())

        if scheduler_rollback_start:
            from apps.ai_pipeline.services import LectureProcessingOrchestrator

            LectureProcessingOrchestrator().start(lecture=job.lecture, task_id=task_id)

        try:
            from apps.ai_pipeline.services import launch_lecture_processing

            launch_lecture_processing(lecture_id=lecture_id, task_id=task_id, job_id=job_id)
        except Exception:  # pragma: no cover - requires a broker dispatch failure
            logger = logging.getLogger("tesanote")
            logger.exception(
                "processing_job_dispatch_failed",
                extra={"job_id": job_id, "lecture_id": lecture_id},
            )
            cls.mark_dispatch_failed(job_id=job_id)
            from apps.ai_pipeline.exceptions import PipelineError
            from apps.ai_pipeline.services import LectureProcessingOrchestrator

            lecture = Lecture.objects.select_related("user").filter(id=lecture_id).first()
            if lecture is not None:
                LectureProcessingOrchestrator().fail(
                    lecture=lecture,
                    error=PipelineError(
                        stage="PIPELINE_DISPATCH",
                        message="The processing job could not be started. Please try again.",
                    ),
                )

    @classmethod
    def mark_started(cls, *, job_id: str, task_id: str = "") -> LectureProcessingJob | None:
        queue_wait_ms: int | None = None
        scheduler_wait_ms: int | None = None
        with transaction.atomic():
            job = LectureProcessingJob.objects.select_for_update().filter(id=job_id).first()
            if job is None:
                return None
            job._scheduler_started_now = False
            if job.status == LectureProcessingJobStatus.QUEUED:
                now = timezone.now()
                queue_wait_ms = max(0, round((now - job.queued_at).total_seconds() * 1000))
                if job.scheduler_managed and job.scheduler_ready_at is not None:
                    scheduler_wait_ms = max(
                        0, round((now - job.scheduler_ready_at).total_seconds() * 1000)
                    )
                job.status = LectureProcessingJobStatus.PROCESSING
                job.stage = "PIPELINE"
                job.started_at = now
                job.last_heartbeat_at = now
                if task_id:
                    job.celery_task_id = task_id
                job.save(
                    update_fields=[
                        "status",
                        "stage",
                        "started_at",
                        "last_heartbeat_at",
                        "celery_task_id",
                        "updated_at",
                    ]
                )
                job._scheduler_started_now = True

        if queue_wait_ms is not None:
            with measure_scaling_stage(
                stage="processing_queue_wait",
                lecture_id=str(job.lecture_id),
                task_id=job.celery_task_id,
                job_id=str(job.id),
                queue_wait_ms=queue_wait_ms,
            ):
                pass
        if scheduler_wait_ms is not None:
            broker_wait_ms = 0
            if job.scheduler_dispatched_at is not None and job.started_at is not None:
                broker_wait_ms = max(
                    0, round((job.started_at - job.scheduler_dispatched_at).total_seconds() * 1000)
                )
            with measure_scaling_stage(
                stage="fair_scheduler_wait",
                lecture_id=str(job.lecture_id),
                task_id=job.celery_task_id,
                job_id=str(job.id),
                scheduler_wait_ms=scheduler_wait_ms,
                broker_wait_ms=broker_wait_ms,
            ):
                pass
        return job

    @staticmethod
    def _is_fenced(*, job: LectureProcessingJob, expected_generation: int | None) -> bool:
        """True when `job` has moved past the execution attempt the caller
        started with — i.e. Phase 12 recovery already reclaimed this job and
        the caller is an obsolete, superseded attempt that must not be
        allowed to mutate authoritative state.

        `expected_generation=None` means the caller never captured a
        generation (most existing call sites, and any job that has never
        been through recovery) — those are intentionally not fenced, since
        there is nothing to fence against and Phase 12 must not change
        behavior for the common case.
        """
        return expected_generation is not None and job.execution_generation != expected_generation

    @classmethod
    def is_current_execution(cls, *, lecture_id: str, expected_generation: int | None) -> bool:
        """Fast, lock-free fencing pre-check for callers about to do real work.

        Used to skip lecture-level side effects (marking the lecture failed
        or completed, finalizing usage logs, dispatching follow-up work)
        entirely for an obsolete attempt, rather than only preventing the
        final job-status write. `mark_succeeded_for_lecture` /
        `mark_failed_for_lecture` remain the authoritative, lock-protected
        fencing boundary; this is a cheap early-exit, not a replacement.
        """
        if expected_generation is None:
            return True
        job = (
            LectureProcessingJob.objects.filter(
                lecture_id=lecture_id, status__in=ACTIVE_JOB_STATUSES
            )
            .order_by("-attempt")
            .first()
        )
        if job is None:
            return False
        return job.execution_generation == expected_generation

    @classmethod
    def mark_finalizing_for_lecture(
        cls, *, lecture_id: str, expected_generation: int | None = None
    ) -> LectureProcessingJob | None:
        with transaction.atomic():
            job = (
                LectureProcessingJob.objects.select_for_update()
                .filter(lecture_id=lecture_id, status__in=ACTIVE_JOB_STATUSES)
                .order_by("-attempt")
                .first()
            )
            if job is None:
                return None
            if cls._is_fenced(job=job, expected_generation=expected_generation):
                logger.warning(
                    "processing_fenced_obsolete_attempt",
                    extra={
                        "job_id": str(job.id),
                        "lecture_id": lecture_id,
                        "call": "mark_finalizing_for_lecture",
                        "job_generation": job.execution_generation,
                        "caller_generation": expected_generation,
                    },
                )
                return None
            if job.status == LectureProcessingJobStatus.PROCESSING and job.stage != "FINALIZING":
                job.stage = "FINALIZING"
                job.save(update_fields=["stage", "updated_at"])
            return job

    @classmethod
    def mark_succeeded_for_lecture(
        cls, *, lecture_id: str, expected_generation: int | None = None
    ) -> LectureProcessingJob | None:
        execution_time_ms: int | None = None
        user_id = ""
        with transaction.atomic():
            job = (
                LectureProcessingJob.objects.select_for_update()
                .filter(lecture_id=lecture_id, status__in=ACTIVE_JOB_STATUSES)
                .order_by("-attempt")
                .first()
            )
            if job is None:
                return None
            if cls._is_fenced(job=job, expected_generation=expected_generation):
                logger.warning(
                    "processing_fenced_obsolete_attempt",
                    extra={
                        "job_id": str(job.id),
                        "lecture_id": lecture_id,
                        "call": "mark_succeeded_for_lecture",
                        "job_generation": job.execution_generation,
                        "caller_generation": expected_generation,
                    },
                )
                with measure_scaling_stage(
                    stage="processing_fenced_obsolete_attempt",
                    lecture_id=lecture_id,
                    job_id=str(job.id),
                    call="mark_succeeded_for_lecture",
                ):
                    pass
                return None
            now = timezone.now()
            job.status = LectureProcessingJobStatus.SUCCEEDED
            job.stage = "COMPLETED"
            job.progress = 100
            job.finished_at = now
            if job.started_at is not None:
                execution_time_ms = max(0, round((now - job.started_at).total_seconds() * 1000))
            job.save(update_fields=["status", "stage", "progress", "finished_at", "updated_at"])
            user_id = str(job.user_id)
            cls._schedule_temporary_artifact_expiration(
                lecture_id=str(job.lecture_id), finished_at=now
            )

        if execution_time_ms is not None:
            with measure_scaling_stage(
                stage="processing_execution_time",
                lecture_id=str(job.lecture_id),
                task_id=job.celery_task_id,
                job_id=str(job.id),
                execution_time_ms=execution_time_ms,
            ):
                pass
        if user_id:
            cls._schedule_pending_promotion(user_id=user_id)
        return job

    @classmethod
    def mark_failed_for_lecture(
        cls, *, lecture_id: str, stage: str, expected_generation: int | None = None
    ) -> LectureProcessingJob | None:
        category = cls._failure_category(stage=stage)
        user_id = ""
        with transaction.atomic():
            job = (
                LectureProcessingJob.objects.select_for_update()
                .filter(lecture_id=lecture_id, status__in=ACTIVE_JOB_STATUSES)
                .order_by("-attempt")
                .first()
            )
            if job is None:
                return None
            if cls._is_fenced(job=job, expected_generation=expected_generation):
                logger.warning(
                    "processing_fenced_obsolete_attempt",
                    extra={
                        "job_id": str(job.id),
                        "lecture_id": lecture_id,
                        "call": "mark_failed_for_lecture",
                        "job_generation": job.execution_generation,
                        "caller_generation": expected_generation,
                    },
                )
                with measure_scaling_stage(
                    stage="processing_fenced_obsolete_attempt",
                    lecture_id=lecture_id,
                    job_id=str(job.id),
                    call="mark_failed_for_lecture",
                ):
                    pass
                return None
            job.status = LectureProcessingJobStatus.FAILED
            job.stage = "FAILED"
            job.finished_at = timezone.now()
            job.failure_category = category
            job.save(
                update_fields=["status", "stage", "finished_at", "failure_category", "updated_at"]
            )
            user_id = str(job.user_id)
            cls._schedule_temporary_artifact_expiration(
                lecture_id=str(job.lecture_id), finished_at=job.finished_at
            )
        if user_id:
            cls._schedule_pending_promotion(user_id=user_id)
        return job

    @classmethod
    def mark_pending_failed(cls, *, job_id: str) -> LectureProcessingJob | None:
        """Terminally reject a pending item when preflight cannot be completed."""
        with transaction.atomic():
            job = LectureProcessingJob.objects.select_for_update().filter(id=job_id).first()
            if job is None or job.status != PENDING_JOB_STATUS:
                return job
            job.status = LectureProcessingJobStatus.FAILED
            job.stage = "FAILED"
            job.finished_at = timezone.now()
            job.failure_category = LectureProcessingFailureCategory.INTERNAL_ERROR
            job.save(
                update_fields=["status", "stage", "finished_at", "failure_category", "updated_at"]
            )
            cls._schedule_temporary_artifact_expiration(
                lecture_id=str(job.lecture_id), finished_at=job.finished_at
            )
            return job

    @classmethod
    def mark_dispatch_failed(cls, *, job_id: str) -> LectureProcessingJob | None:
        user_id = ""
        with transaction.atomic():
            job = LectureProcessingJob.objects.select_for_update().filter(id=job_id).first()
            if job is None or job.status not in ACTIVE_JOB_STATUSES:
                return job
            job.status = LectureProcessingJobStatus.FAILED
            job.stage = "FAILED"
            job.finished_at = timezone.now()
            job.failure_category = LectureProcessingFailureCategory.DISPATCH_FAILED
            job.save(
                update_fields=["status", "stage", "finished_at", "failure_category", "updated_at"]
            )
            user_id = str(job.user_id)
            cls._schedule_temporary_artifact_expiration(
                lecture_id=str(job.lecture_id), finished_at=job.finished_at
            )
        if user_id:
            cls._schedule_pending_promotion(user_id=user_id)
        return job

    @classmethod
    def evaluate_and_recover_stale_job(cls, *, job_id: str) -> str:
        """Phase 12: the one concurrency-safe transaction behind stale-job recovery.

        Runs entirely under a single row lock: lock -> re-read heartbeat/status
        fresh -> confirm still PROCESSING and still stale -> classify the
        lecture's stage -> fence the old attempt and transition -> commit.
        Talking to Celery/failing the lecture happens only after commit, and
        the requeue path returns through the existing dispatch_queued_processing
        entry point — recovery never opens a second direct-dispatch path.

        Returns one of "recovered", "failed_permanently", "skipped".
        """
        from apps.ai_pipeline.recovery import AMBIGUOUS, classify_stage

        new_task_id = str(uuid4())
        outcome = "skipped"
        lecture_id = ""
        user_id = ""

        with transaction.atomic():
            job = (
                LectureProcessingJob.objects.select_for_update()
                .select_related("lecture")
                .filter(id=job_id, status=LectureProcessingJobStatus.PROCESSING)
                .first()
            )
            if job is None:
                return "skipped"

            cutoff = timezone.now() - timedelta(
                seconds=int(getattr(settings, "PROCESSING_HEARTBEAT_STALE_SECONDS", 300))
            )
            is_stale = job.last_heartbeat_at is None or job.last_heartbeat_at < cutoff
            if not is_stale:
                # A heartbeat landed between candidate selection and this lock.
                return "skipped"

            classification = classify_stage(lecture=job.lecture)
            exhausted = job.recovery_count >= int(
                getattr(settings, "PROCESSING_MAX_RECOVERY_ATTEMPTS", 2)
            )

            if exhausted or classification == AMBIGUOUS:
                reason = (
                    "MAX_RECOVERY_ATTEMPTS_EXCEEDED"
                    if exhausted
                    else "AMBIGUOUS_PROVIDER_OUTCOME"
                )
                now = timezone.now()
                job.execution_generation += 1
                job.status = LectureProcessingJobStatus.FAILED
                job.stage = "FAILED"
                job.finished_at = now
                job.failure_category = LectureProcessingFailureCategory.RECOVERY_REQUIRED
                job.last_recovery_reason = reason
                job.last_recovery_at = now
                job.save(
                    update_fields=[
                        "execution_generation",
                        "status",
                        "stage",
                        "finished_at",
                        "failure_category",
                        "last_recovery_reason",
                        "last_recovery_at",
                        "updated_at",
                    ]
                )
                outcome = "failed_permanently"
                lecture_id = str(job.lecture_id)
                user_id = str(job.user_id)
                cls._schedule_temporary_artifact_expiration(
                    lecture_id=lecture_id, finished_at=now
                )
                logger.warning(
                    "processing_recovery_failed_permanently",
                    extra={
                        "job_id": str(job.id),
                        "lecture_id": lecture_id,
                        "reason": reason,
                        "recovery_count": job.recovery_count,
                    },
                )
            else:
                now = timezone.now()
                job.execution_generation += 1
                job.status = LectureProcessingJobStatus.QUEUED
                job.started_at = None
                job.finished_at = None
                job.celery_task_id = new_task_id
                job.queued_at = now
                job.recovery_count += 1
                job.last_recovery_reason = "WORKER_HEARTBEAT_STALE"
                job.last_recovery_at = now
                update_fields = [
                    "execution_generation",
                    "status",
                    "started_at",
                    "finished_at",
                    "celery_task_id",
                    "queued_at",
                    "recovery_count",
                    "last_recovery_reason",
                    "last_recovery_at",
                    "updated_at",
                ]
                if job.scheduler_managed:
                    job.stage = "SCHEDULER_WAIT"
                    job.scheduler_ready_at = now
                    job.scheduler_claimed_at = None
                    job.scheduler_dispatched_at = None
                    update_fields += [
                        "stage",
                        "scheduler_ready_at",
                        "scheduler_claimed_at",
                        "scheduler_dispatched_at",
                    ]
                else:
                    job.stage = "QUEUED"
                    update_fields.append("stage")
                job.save(update_fields=update_fields)
                outcome = "recovered"
                lecture_id = str(job.lecture_id)
                logger.warning(
                    "processing_recovery_requeued",
                    extra={
                        "job_id": str(job.id),
                        "lecture_id": lecture_id,
                        "recovery_count": job.recovery_count,
                        "execution_generation": job.execution_generation,
                    },
                )

        with measure_scaling_stage(
            stage="processing_recovery_candidate",
            job_id=job_id,
            lecture_id=lecture_id,
            outcome=outcome,
        ):
            pass

        if outcome == "recovered":
            cls.dispatch_queued_processing(
                job_id=job_id, lecture_id=lecture_id, task_id=new_task_id
            )
        elif outcome == "failed_permanently":
            from apps.ai_pipeline.timeouts import cancel_lecture_processing
            from apps.lectures.models import Lecture as LectureModel

            lecture = LectureModel.objects.filter(id=lecture_id).first()
            if lecture is not None:
                cancel_lecture_processing(
                    lecture=lecture,
                    error_message=(
                        "پردازش این جزوه به دلیل قطع ارتباط با پردازشگر متوقف شد. "
                        "لطفاً دوباره تلاش کنید."
                    ),
                    reason_code="processing_recovery_required",
                )
            if user_id:
                cls._schedule_pending_promotion(user_id=user_id)

        return outcome
