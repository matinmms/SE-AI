from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.ai_pipeline.capacity import (
    DistributedCapacityController,
    DynamicCapacityController,
    ResourceSnapshotStore,
)
from apps.ai_pipeline.models import (
    FairSchedulerDispatchControl,
    LectureProcessingJob,
    LectureProcessingJobStatus,
)
from infrastructure.observability.scaling import measure_scaling_stage

logger = logging.getLogger("tesanote.fair_scheduler")


@dataclass(frozen=True)
class SchedulerDispatchResult:
    claimed_job_ids: list[str]
    recovered_stale_claims: int
    active_slots: int
    available_slots: int


class FairSchedulerService:
    """Durable, FIFO-across-users scheduling for logical lecture jobs.

    Each user can already own at most one QUEUED/PROCESSING job. Therefore the
    oldest ready job is also the oldest eligible user, which gives us a small,
    explainable starvation-resistant policy without another per-user queue.
    """

    @staticmethod
    def enabled() -> bool:
        return bool(getattr(settings, "FAIR_SCHEDULER_ENABLED", False))

    @staticmethod
    def distributed_enabled() -> bool:
        return bool(getattr(settings, "DISTRIBUTED_CAPACITY_ENABLED", False))

    @classmethod
    def capacity_decision(cls):
        """Single-host decision, unchanged. Callers that need the Phase 16
        distributed view (multi-host aggregation, per-host breakdown) must
        call `distributed_capacity_decision()` explicitly and check
        `distributed_enabled()` first — this keeps every existing call site
        byte-for-byte identical when the flag is off."""
        return DynamicCapacityController.decide()

    @classmethod
    def distributed_capacity_decision(cls):
        return DistributedCapacityController.aggregate()

    @classmethod
    def max_concurrent_jobs(cls) -> int:
        if cls.distributed_enabled():
            return cls.distributed_capacity_decision().effective_capacity
        return cls.capacity_decision().effective_capacity

    @staticmethod
    def claim_timeout() -> timedelta:
        return timedelta(
            seconds=max(30, int(getattr(settings, "FAIR_SCHEDULER_CLAIM_TIMEOUT_SECONDS", 300)))
        )

    @classmethod
    def ready_queryset(cls):
        return LectureProcessingJob.objects.filter(
            status=LectureProcessingJobStatus.QUEUED,
            scheduler_managed=True,
            scheduler_ready_at__isnull=False,
            scheduler_dispatched_at__isnull=True,
            scheduler_claimed_at__isnull=True,
        )

    @classmethod
    def active_slots_queryset(cls, *, host_id: str | None = None):
        """Logical jobs that already consume one global execution slot.

        `host_id`, when given, narrows this to jobs assigned to that
        specific worker host (Phase 16) — a job dispatched before
        distributed mode was enabled has a blank `assigned_worker_host_id`
        and is attributed to this process's own host id, since that is
        where all pre-Phase-16 work actually ran.
        """
        qs = LectureProcessingJob.objects.filter(
            Q(status=LectureProcessingJobStatus.PROCESSING)
            | Q(
                status=LectureProcessingJobStatus.QUEUED,
                scheduler_managed=False,
            )
            | Q(
                status=LectureProcessingJobStatus.QUEUED,
                scheduler_managed=True,
                scheduler_claimed_at__isnull=False,
            )
        )
        if host_id is not None:
            own_host = str(getattr(settings, "PROCESSING_WORKER_HOST_ID", "web-local"))
            if host_id == own_host:
                qs = qs.filter(Q(assigned_worker_host_id=host_id) | Q(assigned_worker_host_id=""))
            else:
                qs = qs.filter(assigned_worker_host_id=host_id)
        return qs

    @classmethod
    def active_slot_count(cls, *, host_id: str | None = None) -> int:
        return cls.active_slots_queryset(host_id=host_id).count()

    @classmethod
    def available_slots(cls) -> int:
        return max(0, cls.max_concurrent_jobs() - cls.active_slot_count())

    @classmethod
    def kick_after_commit(cls) -> None:
        """Best-effort wakeup; Beat reconciliation keeps durable ready work safe."""
        transaction.on_commit(cls.kick)

    @classmethod
    def kick(cls) -> None:
        try:
            from apps.ai_pipeline.tasks import dispatch_available_processing_jobs

            dispatch_available_processing_jobs.delay()
        except Exception:  # pragma: no cover - broker failure is recovered by Beat
            logger.exception("fair_scheduler_kick_failed")

    @classmethod
    def _control_for_update(cls) -> FairSchedulerDispatchControl:
        try:
            FairSchedulerDispatchControl.objects.get_or_create(id=1)
        except IntegrityError:
            # Another scheduler invocation created the singleton; lock it below.
            pass
        return FairSchedulerDispatchControl.objects.select_for_update().get(id=1)

    @classmethod
    def _recover_stale_claims_locked(cls, *, now) -> int:
        cutoff = now - cls.claim_timeout()
        stale = LectureProcessingJob.objects.filter(
            status=LectureProcessingJobStatus.QUEUED,
            scheduler_managed=True,
            scheduler_claimed_at__lt=cutoff,
            started_at__isnull=True,
        )
        return stale.update(
            scheduler_claimed_at=None,
            scheduler_dispatched_at=None,
            scheduler_last_dispatch_error="STALE_CLAIM_RECOVERED",
            stage="SCHEDULER_WAIT",
        )

    @classmethod
    def dispatch_available_jobs(cls) -> dict[str, int]:
        """Claim ready jobs under one short DB lock, then dispatch after commit."""
        if not cls.enabled():
            legacy_dispatched = cls.dispatch_ready_jobs_via_legacy_path()
            return {
                "disabled": 1,
                "legacy_dispatched": legacy_dispatched,
                "claimed": 0,
                "recovered": 0,
                "active_slots": cls.active_slot_count(),
                "available_slots": cls.available_slots(),
            }

        now = timezone.now()
        distributed = cls.distributed_enabled()
        claimed_job_ids: list[str] = []
        host_remaining: dict[str, int] = {}
        with transaction.atomic():
            cls._control_for_update()
            recovered = cls._recover_stale_claims_locked(now=now)

            if distributed:
                decision = cls.distributed_capacity_decision()
                # Per-host remaining capacity, computed under the same lock
                # as the claim below so two racing ticks can't both see the
                # same "remaining" number for a host.
                for host_status in decision.per_host:
                    active_for_host = cls.active_slot_count(host_id=host_status.host_id)
                    host_remaining[host_status.host_id] = max(
                        0, host_status.safe_slots - active_for_host
                    )
                active_slots = sum(
                    cls.active_slot_count(host_id=h.host_id) for h in decision.per_host
                )
                available_slots = min(
                    max(0, decision.effective_capacity - active_slots),
                    sum(host_remaining.values()),
                )
            else:
                active_slots = cls.active_slots_queryset().count()
                available_slots = max(0, cls.max_concurrent_jobs() - active_slots)

            if available_slots:
                candidates = list(
                    cls.ready_queryset()
                    .select_for_update(skip_locked=True)
                    .order_by("scheduler_ready_at", "created_at", "id")[:available_slots]
                )
                # Stable host order so which host absorbs the remainder is
                # deterministic and auditable, not incidental dict ordering.
                host_order = sorted(host_remaining) if distributed else []
                for job in candidates:
                    assigned_host = ""
                    if distributed:
                        assigned_host = next(
                            (h for h in host_order if host_remaining.get(h, 0) > 0), ""
                        )
                        if not assigned_host:
                            # Every registered host is already at its own cap
                            # even though the global sum looked sufficient
                            # (e.g. concurrent accounting drift) — leave this
                            # job unclaimed rather than over-assign.
                            break
                        host_remaining[assigned_host] -= 1
                    job.scheduler_claimed_at = now
                    job.scheduler_dispatch_attempts += 1
                    job.scheduler_last_dispatch_error = ""
                    job.stage = "SCHEDULER_CLAIMED"
                    update_fields = [
                        "scheduler_claimed_at",
                        "scheduler_dispatch_attempts",
                        "scheduler_last_dispatch_error",
                        "stage",
                        "updated_at",
                    ]
                    if distributed:
                        job.assigned_worker_host_id = assigned_host
                        update_fields.append("assigned_worker_host_id")
                    job.save(update_fields=update_fields)
                    claimed_job_ids.append(str(job.id))

        if recovered:
            with measure_scaling_stage(
                stage="fair_scheduler_recovered_stranded_job", recovered_count=recovered
            ):
                pass
        if not claimed_job_ids:
            metric = (
                "fair_scheduler_no_capacity"
                if available_slots == 0
                else "fair_scheduler_no_eligible_jobs"
            )
            with measure_scaling_stage(
                stage=metric,
                active_slots=active_slots,
                available_slots=available_slots,
            ):
                pass

        for job_id in claimed_job_ids:
            cls._dispatch_claimed_job(job_id=job_id)

        return {
            "disabled": 0,
            "legacy_dispatched": 0,
            "claimed": len(claimed_job_ids),
            "recovered": recovered,
            "active_slots": active_slots,
            "available_slots": available_slots,
        }

    @classmethod
    def _dispatch_claimed_job(cls, *, job_id: str) -> None:
        with transaction.atomic():
            job = (
                LectureProcessingJob.objects.select_for_update()
                .select_related("lecture")
                .filter(id=job_id, status=LectureProcessingJobStatus.QUEUED, scheduler_managed=True)
                .first()
            )
            if (
                job is None
                or job.scheduler_claimed_at is None
                or job.scheduler_dispatched_at is not None
            ):
                return
            dispatched_at = timezone.now()
            job.scheduler_dispatched_at = dispatched_at
            job.stage = "SCHEDULER_DISPATCHED"
            job.save(update_fields=["scheduler_dispatched_at", "stage", "updated_at"])
            lecture_id = str(job.lecture_id)
            task_id = job.celery_task_id
            assigned_host = job.assigned_worker_host_id or None

        try:
            from apps.ai_pipeline.services import launch_lecture_processing

            launch_lecture_processing(
                lecture_id=lecture_id,
                task_id=task_id,
                job_id=str(job.id),
                worker_host_id=assigned_host,
            )
        except Exception as exc:  # dispatch is recoverable; do not fail the lecture yet.
            cls.release_dispatch_claim(job_id=str(job.id), error=exc.__class__.__name__)
            logger.exception("fair_scheduler_dispatch_failed", extra={"job_id": str(job.id)})
            with measure_scaling_stage(
                stage="fair_scheduler_dispatch_failed",
                lecture_id=lecture_id,
                job_id=str(job.id),
                error_class=exc.__class__.__name__,
            ):
                pass
            return

        with measure_scaling_stage(
            stage="fair_scheduler_dispatch",
            lecture_id=lecture_id,
            task_id=task_id,
            job_id=str(job.id),
        ):
            pass

    @classmethod
    def release_dispatch_claim(cls, *, job_id: str, error: str) -> None:
        with transaction.atomic():
            job = LectureProcessingJob.objects.select_for_update().filter(id=job_id).first()
            if job is None or job.status != LectureProcessingJobStatus.QUEUED:
                return
            job.scheduler_claimed_at = None
            job.scheduler_dispatched_at = None
            job.scheduler_last_dispatch_error = error[:128]
            job.stage = "SCHEDULER_WAIT"
            job.save(
                update_fields=[
                    "scheduler_claimed_at",
                    "scheduler_dispatched_at",
                    "scheduler_last_dispatch_error",
                    "stage",
                    "updated_at",
                ]
            )

    @classmethod
    def dispatch_ready_jobs_via_legacy_path(cls) -> int:
        """Rollback bridge: drain scheduler-ready jobs through the prior dispatch path."""
        with transaction.atomic():
            cls._control_for_update()
            jobs = list(
                cls.ready_queryset()
                .select_for_update(skip_locked=True)
                .order_by("scheduler_ready_at", "created_at", "id")
            )
            for job in jobs:
                job.scheduler_claimed_at = None
                job.scheduler_last_dispatch_error = ""
                # Keep the managed flag until ProcessingJobService performs the
                # legacy handoff. It owns the old start-before-dispatch contract.
                job.stage = "SCHEDULER_WAIT"
                job.save(
                    update_fields=[
                        "scheduler_claimed_at",
                        "scheduler_last_dispatch_error",
                        "stage",
                        "updated_at",
                    ]
                )

        for job in jobs:
            from apps.ai_pipeline.jobs import ProcessingJobService

            ProcessingJobService.dispatch_queued_processing(
                job_id=str(job.id), lecture_id=str(job.lecture_id), task_id=job.celery_task_id
            )
        return len(jobs)

    @classmethod
    def admin_summary(cls) -> dict[str, object]:
        now = timezone.now()
        ready = cls.ready_queryset()
        oldest_ready = (
            ready.order_by("scheduler_ready_at")
            .values_list("scheduler_ready_at", flat=True)
            .first()
        )
        today = timezone.localdate()
        decision = cls.capacity_decision()
        return {
            "enabled": cls.enabled(),
            "configured_slots": decision.effective_capacity,
            "active_jobs": cls.active_slot_count(),
            "ready_jobs": ready.count(),
            "available_slots": cls.available_slots(),
            "oldest_wait_seconds": max(0, int((now - oldest_ready).total_seconds()))
            if oldest_ready
            else None,
            "dispatches_today": LectureProcessingJob.objects.filter(
                scheduler_dispatched_at__date=today
            ).count(),
            "dispatch_failures": LectureProcessingJob.objects.filter(
                scheduler_last_dispatch_error__gt="",
                status=LectureProcessingJobStatus.QUEUED,
            ).count(),
            "capacity": {
                "dynamic_enabled": bool(getattr(settings, "DYNAMIC_CONCURRENCY_ENABLED", False)),
                "minimum": DynamicCapacityController.minimum(),
                "maximum": DynamicCapacityController.maximum(),
                "hard_maximum": DynamicCapacityController.hard_maximum(),
                **decision.as_admin_payload(),
                "history": ResourceSnapshotStore.recent_history(),
            },
        }

    @classmethod
    def waiting_reason(cls, *, job: LectureProcessingJob) -> str:
        if job.status == LectureProcessingJobStatus.PENDING:
            return "PENDING_BEHIND_USER_JOB"
        if job.status == LectureProcessingJobStatus.PROCESSING:
            return "PROCESSING"
        if job.scheduler_managed and job.scheduler_dispatched_at is None:
            if job.scheduler_last_dispatch_error:
                return "DISPATCH_RETRY"
            return "GLOBAL_CAPACITY" if cls.available_slots() <= 0 else "SCHEDULER_READY"
        if job.status == LectureProcessingJobStatus.QUEUED:
            return "BROKER_OR_WORKER_QUEUE"
        return "TERMINAL"
