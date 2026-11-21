from __future__ import annotations

import logging
from datetime import timedelta
from threading import Thread
from uuid import uuid4

from celery import shared_task
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from apps.ai_pipeline.exceptions import PipelineError
from apps.ai_pipeline.timeouts import enforce_lecture_processing_timeouts
from apps.ai_pipeline.services import SlideProcessingService
from apps.ai_pipeline.stage_metrics import StageConcurrencyStore
from apps.lectures.models import Lecture, LectureProcessingState, LectureStatus, SlideProcessingStatus
from apps.uploads.models import LectureFileType, UploadStatus
from apps.notes.generation import (
    NoteGenerationDebugService,
    NoteGenerationOrchestrator,
    NoteGenerationReadinessService,
    normalize_note_variant,
    selected_note_models_for_lecture,
)
from apps.notes.models import GeneratedNote, GeneratedNoteSummary, NoteGenerationStatus, NoteStudyDifficulty
from apps.notes.services import GeneratedNoteService
from apps.notes.study_items import NoteStudyGenerationService, StudyItemsError
from apps.notes.summaries import NoteSummaryService
from infrastructure.observability.scaling import measure_scaling_stage
from infrastructure.queue.control import lecture_task_lock

logger = logging.getLogger(__name__)

AUTO_STUDY_OUTPUTS = frozenset({"flashcards", "quiz"})
AUTO_SELECTED_OUTPUTS = frozenset({"summary", *AUTO_STUDY_OUTPUTS})
AUTO_STUDY_ITEM_COUNT = 5


def _actual_delivery_queue(self, *, default: str) -> str:
    """The Celery queue a bound task was actually consumed from, read from
    its own delivery info rather than assumed from a hardcoded label --
    needed because `generate_note_task` is dispatched to different queues
    depending on caller (batch lecture completion vs. interactive
    regenerate/summary, Phase 20). Falls back to `default` when delivery
    info is unavailable (e.g. eager/test-mode invocation, no real broker
    delivery).
    """
    return (self.request.delivery_info or {}).get("routing_key") or default


def _completed_primary_note(*, lecture: Lecture) -> GeneratedNote | None:
    selected_models = selected_note_models_for_lecture(lecture=lecture)
    primary_variant = normalize_note_variant(selected_models[0]) if selected_models else ""
    completed_notes = GeneratedNote.objects.filter(
        lecture=lecture,
        status=NoteGenerationStatus.COMPLETED,
    ).order_by("created_at")
    if primary_variant:
        primary_note = completed_notes.filter(variant=primary_variant).first()
        if primary_note is not None:
            return primary_note
    return completed_notes.first()


def _selected_study_outputs(*, lecture: Lecture) -> set[str]:
    return {
        str(output)
        for output in (lecture.processing_metadata or {}).get("study_outputs", []) or []
    }


def _queue_selected_summary(*, lecture: Lecture, note: GeneratedNote) -> GeneratedNoteSummary | None:
    """Create the requested summary record before handing it to the notes worker."""
    try:
        summary = NoteSummaryService().request_generation(note=note, user=lecture.user)
    except Exception:
        logger.exception("selected_summary_generation_queue_failed", extra={"lecture_id": str(lecture.id)})
        return None

    if summary.status == NoteGenerationStatus.QUEUED:
        generate_note_summary_task.delay(str(summary.id))
    return summary


def lecture_processing_stopped(*, lecture: Lecture) -> bool:
    return lecture.status in {LectureStatus.FAILED, LectureStatus.COMPLETED} or lecture.processing_state == LectureProcessingState.FAILED


def run_note_generation_sync(*, lecture_id: str, transcript_override: str | None = None) -> dict[str, object]:
    lecture = Lecture.objects.get(id=lecture_id)
    if lecture_processing_stopped(lecture=lecture):
        return NoteGenerationDebugService.debug_payload(lecture=lecture)
    timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
    if timeout_error is not None:
        return NoteGenerationDebugService.debug_payload(lecture=Lecture.objects.get(id=lecture_id))
    selected_model = str((lecture.processing_metadata or {}).get("note_generation_selected_model", "") or "").strip()
    if not selected_model:
        NoteGenerationReadinessService().persist_selection(lecture=lecture)
        lecture.refresh_from_db()
    service = NoteGenerationOrchestrator(models=selected_note_models_for_lecture(lecture=lecture))
    from apps.ai_costs.context import avalai_cost_context
    from apps.ai_pipeline.models import LectureProcessingJob

    job = LectureProcessingJob.objects.filter(lecture=lecture).order_by("-created_at").first()
    # Phase 12 fencing: capture the generation as of *this task's* start. If
    # recovery later reclaims this job while this task is still executing, its
    # eventual mark_succeeded_for_lecture/mark_failed_for_lecture calls will
    # carry this now-stale value and be rejected.
    expected_generation = job.execution_generation if job else None
    with avalai_cost_context(
        user_id=str(lecture.user_id),
        lecture_id=str(lecture.id),
        processing_job_id=str(job.id) if job else "",
        feature="note_generation",
    ):
        return service.run_pipeline(
            lecture=lecture,
            transcript_override=transcript_override,
            expected_generation=expected_generation,
        )


def run_note_generation_from_uploaded_transcript_sync(
    *,
    lecture_id: str,
    transcript_override: str,
    prepare_slides: bool = True,
) -> dict[str, object]:
    lecture = Lecture.objects.get(id=lecture_id)
    if lecture_processing_stopped(lecture=lecture):
        return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}
    if prepare_slides:
        has_uploaded_slides = lecture.files.filter(
            file_type=LectureFileType.SLIDES,
            upload_status=UploadStatus.UPLOADED,
        ).exists()
        if has_uploaded_slides and lecture.slide_processing_status != SlideProcessingStatus.COMPLETED:
            _mark_manual_transcript_waiting_for_slides(lecture=lecture)
            slide_service = SlideProcessingService()
            slide_service.start_processing(lecture=lecture, task_id=f"manual-note-{uuid4()}")
            slide_service.run_pipeline(lecture=lecture, lightweight=True)
            lecture.refresh_from_db()
    return run_note_generation_sync(lecture_id=lecture_id, transcript_override=transcript_override)


@shared_task(bind=True, autoretry_for=(PipelineError,), retry_backoff=True, retry_kwargs={"max_retries": 2}, ignore_result=True)
def generate_note_task(self, lecture_id: str, transcript_override: str | None = None) -> dict[str, object]:
    actual_queue = _actual_delivery_queue(self, default="notes")
    with StageConcurrencyStore.track_stage(
        queue=actual_queue, task_id=self.request.id or lecture_id
    ):
        return _generate_note_task_body(self, lecture_id, transcript_override)


def _generate_note_task_body(
    self, lecture_id: str, transcript_override: str | None = None
) -> dict[str, object]:
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="note-generation",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info("note_generation_already_running", extra={"lecture_id": lecture_id})
            lecture = Lecture.objects.get(id=lecture_id)
            return NoteGenerationDebugService.debug_payload(lecture=lecture)
        lecture = Lecture.objects.get(id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return NoteGenerationDebugService.debug_payload(lecture=lecture)
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            return NoteGenerationDebugService.debug_payload(lecture=Lecture.objects.get(id=lecture_id))
        with measure_scaling_stage(
            stage="note_generation",
            lecture_id=lecture_id,
            task_id=self.request.id,
            task_name=self.name,
        ):
            return run_note_generation_sync(lecture_id=lecture_id, transcript_override=transcript_override)


@shared_task(ignore_result=True)
def generate_comparison_v2_note_task(lecture_id: str) -> dict[str, object]:
    """Admin comparison: write the v2 (lecturer's own words) note next to the finished v4 note.

    Never retried automatically: each run is a paid note generation for an internal account.
    """
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="note-comparison-v2",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}
        lecture = Lecture.objects.get(id=lecture_id)
        note = NoteGenerationOrchestrator().run_comparison_v2_note(lecture=lecture)
        if note is None:
            return {"lecture_id": lecture_id, "status": "SKIPPED_OR_FAILED"}
        return {"lecture_id": lecture_id, "status": note.status, "note_id": str(note.id)}


@shared_task(ignore_result=True)
def generate_note_version_task(lecture_id: str, style: str) -> dict[str, object]:
    """Write a finished lecture again in another note style, as an extra note (never retried: it is paid)."""
    from apps.ai_costs.context import avalai_cost_context
    from apps.notes.versions import clear_pending

    with lecture_task_lock(
        lecture_id=lecture_id,
        scope=f"note-version:{style}",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}
        lecture = Lecture.objects.get(id=lecture_id)
        with avalai_cost_context(user_id=str(lecture.user_id), lecture_id=str(lecture.id), processing_job_id="", feature="note_version"):
            note = NoteGenerationOrchestrator(models=selected_note_models_for_lecture(lecture=lecture)).run_alt_version(
                lecture=lecture, style=style
            )
        if note is None:
            return {"lecture_id": lecture_id, "status": "SKIPPED_OR_FAILED"}
        clear_pending(lecture=lecture, style=style)
        return {"lecture_id": lecture_id, "status": note.status, "note_id": str(note.id)}


@shared_task(bind=True, autoretry_for=(PipelineError,), retry_backoff=True, retry_kwargs={"max_retries": 2}, ignore_result=True)
def generate_note_pdf_task(self, note_id: str) -> dict[str, object]:
    note = GeneratedNote.objects.select_related("lecture", "user").get(id=note_id)
    with lecture_task_lock(
        lecture_id=str(note.lecture_id),
        scope=f"pdf-artifact:{note_id}",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            return {"note_id": note_id, "status": "ALREADY_RUNNING"}
        note.refresh_from_db()
        if note.status != NoteGenerationStatus.COMPLETED:
            return {"note_id": note_id, "status": note.status}
        if GeneratedNoteService.pdf_artifact_is_current(note):
            return {"note_id": note_id, "status": "READY", "pdf_path": note.pdf_path}
        with measure_scaling_stage(
            stage="pdf_render",
            lecture_id=str(note.lecture_id),
            task_id=self.request.id,
            task_name=self.name,
            input_size_bytes=len((note.body_html or "").encode("utf-8")),
        ):
            pdf_path = GeneratedNoteService.ensure_pdf_artifact(note)
        if not pdf_path:
            raise PipelineError(stage="GENERATING_PDF", message="PDF artifact recovery failed.")
        return {"note_id": note_id, "status": "READY", "pdf_path": pdf_path}


@shared_task(bind=True, autoretry_for=(PipelineError,), retry_backoff=True, retry_kwargs={"max_retries": 1}, ignore_result=True)
def generate_note_summary_task(self, summary_id: str) -> dict[str, object]:
    summary = GeneratedNoteSummary.objects.select_related("source_note__lecture").get(id=summary_id)
    with lecture_task_lock(
        lecture_id=str(summary.source_note.lecture_id),
        scope=f"note-summary:{summary_id}",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            return {"summary_id": summary_id, "status": "ALREADY_RUNNING"}
        with measure_scaling_stage(
            stage="summary_generation",
            lecture_id=str(summary.source_note.lecture_id),
            task_id=self.request.id,
            task_name=self.name,
        ):
            from apps.ai_costs.context import avalai_cost_context

            with avalai_cost_context(
                user_id=str(summary.user_id),
                lecture_id=str(summary.source_note.lecture_id),
                feature="summary",
            ):
                generated = NoteSummaryService().generate(summary_id=summary_id)
        return {
            "summary_id": str(generated.id),
            "status": generated.status,
            "progress": generated.progress,
        }


@shared_task(ignore_result=True)
def generate_selected_study_items_task(lecture_id: str) -> dict[str, object]:
    """Generate every selected study output after the primary note is complete."""
    lecture = Lecture.objects.get(id=lecture_id)
    selected_outputs = _selected_study_outputs(lecture=lecture)
    if not selected_outputs.intersection(AUTO_SELECTED_OUTPUTS):
        return {"lecture_id": lecture_id, "status": "SKIPPED"}

    note = _completed_primary_note(lecture=lecture)
    if note is None:
        return {"lecture_id": lecture_id, "status": "NOTE_NOT_READY"}

    summary = None
    if "summary" in selected_outputs:
        summary = _queue_selected_summary(lecture=lecture, note=note)

    if not selected_outputs.intersection(AUTO_STUDY_OUTPUTS):
        return {
            "lecture_id": lecture_id,
            "summary_id": str(summary.id) if summary is not None else None,
            "status": summary.status if summary is not None else "FAILED",
        }

    with lecture_task_lock(
        lecture_id=lecture_id,
        scope=f"study-items:{note.id}",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}
        try:
            with measure_scaling_stage(
                stage="study_items_generation",
                lecture_id=lecture_id,
                task_name="apps.notes.tasks.generate_selected_study_items_task",
            ):
                from apps.ai_costs.context import avalai_cost_context

                with avalai_cost_context(
                    user_id=str(lecture.user_id),
                    lecture_id=str(lecture.id),
                    feature="study_items",
                ):
                    counts = (lecture.processing_metadata or {}).get("study_item_counts") or {}
                    requested_count = max(
                        [int(counts.get(output, AUTO_STUDY_ITEM_COUNT)) for output in selected_outputs & AUTO_STUDY_OUTPUTS],
                        default=AUTO_STUDY_ITEM_COUNT,
                    )
                    study_set = NoteStudyGenerationService().generate(
                        note=note,
                        user=lecture.user,
                        requested_count=requested_count,
                        difficulty=NoteStudyDifficulty.MIXED,
                    )
        except StudyItemsError as exc:
            logger.warning(
                "selected_study_items_generation_failed",
                extra={"lecture_id": lecture_id, "error_message": str(exc)},
            )
            return {"lecture_id": lecture_id, "status": "FAILED"}

    result = {
        "lecture_id": lecture_id,
        "status": study_set.status,
        "generated_count": study_set.generated_count,
    }
    if summary is not None:
        result["summary_id"] = str(summary.id)
    return result


@shared_task(ignore_result=True)
def generate_selected_note_summary_task(lecture_id: str) -> dict[str, object]:
    """Compatibility task for messages queued by earlier application versions."""
    lecture = Lecture.objects.get(id=lecture_id)
    selected_outputs = _selected_study_outputs(lecture=lecture)
    if "summary" not in selected_outputs:
        return {"lecture_id": lecture_id, "status": "SKIPPED"}

    note = _completed_primary_note(lecture=lecture)
    if note is None:
        return {"lecture_id": lecture_id, "status": "NOTE_NOT_READY"}

    summary = _queue_selected_summary(lecture=lecture, note=note)
    if summary is None:
        return {"lecture_id": lecture_id, "status": "FAILED"}

    return {
        "lecture_id": lecture_id,
        "summary_id": str(summary.id),
        "status": summary.status,
    }


@shared_task(ignore_result=True)
def recover_stalled_note_summaries() -> dict[str, int]:
    """Requeue summaries that were accepted while a worker was unavailable."""
    cutoff = timezone.now() - timedelta(seconds=settings.NOTE_SUMMARY_QUEUE_STALL_SECONDS)
    summary_ids: list[str] = []
    failed_count = 0

    with transaction.atomic():
        summaries = list(
            GeneratedNoteSummary.objects.select_for_update(skip_locked=True)
            .filter(status=NoteGenerationStatus.QUEUED, updated_at__lte=cutoff)
            .order_by("updated_at")[: settings.NOTE_SUMMARY_RECOVERY_BATCH_SIZE]
        )
        for summary in summaries:
            metadata = dict(summary.generation_metadata or {})
            attempts = int(metadata.get("queue_recovery_attempts", 0) or 0)
            if attempts >= settings.NOTE_SUMMARY_QUEUE_RECOVERY_MAX_ATTEMPTS:
                summary.status = NoteGenerationStatus.FAILED
                summary.progress = 0
                summary.error_message = "پردازش خلاصه در صف شروع نشد. لطفا دوباره تلاش کنید."
                summary.save(update_fields=["status", "progress", "error_message", "updated_at"])
                failed_count += 1
                continue

            metadata["queue_recovery_attempts"] = attempts + 1
            summary.generation_metadata = metadata
            summary.progress = max(summary.progress, 5)
            summary.save(update_fields=["generation_metadata", "progress", "updated_at"])
            summary_ids.append(str(summary.id))

        if summary_ids:
            transaction.on_commit(
                lambda: [generate_note_summary_task.delay(summary_id) for summary_id in summary_ids]
            )

    return {"requeued_count": len(summary_ids), "failed_count": failed_count}


@shared_task(bind=True, autoretry_for=(PipelineError,), retry_backoff=True, retry_kwargs={"max_retries": 2}, ignore_result=True)
def generate_note_from_uploaded_transcript_task(
    self,
    lecture_id: str,
    transcript_override: str,
    prepare_slides: bool = True,
) -> dict[str, object]:
    with lecture_task_lock(
        lecture_id=lecture_id,
        scope="manual-note-generation",
        ttl_seconds=settings.LECTURE_TASK_LOCK_TTL_SECONDS,
    ) as acquired:
        if not acquired:
            logger.info("manual_note_generation_already_running", extra={"lecture_id": lecture_id})
            return {"lecture_id": lecture_id, "status": "ALREADY_RUNNING"}
        lecture = Lecture.objects.get(id=lecture_id)
        if lecture_processing_stopped(lecture=lecture):
            return {"lecture_id": lecture_id, "status": lecture.status, "stopped": True}
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            return {"lecture_id": lecture_id, "status": "FAILED", "reason": timeout_error.message}
        with measure_scaling_stage(
            stage="note_generation_manual_transcript",
            lecture_id=lecture_id,
            task_id=self.request.id,
            task_name=self.name,
        ):
            return run_note_generation_from_uploaded_transcript_sync(
                lecture_id=lecture_id,
                transcript_override=transcript_override,
                prepare_slides=prepare_slides,
            )


def _run_note_generation_in_local_background(*, lecture_id: str, transcript_override: str | None = None) -> None:
    close_old_connections()
    try:
        run_note_generation_sync(lecture_id=lecture_id, transcript_override=transcript_override)
    finally:
        close_old_connections()


def _run_note_generation_from_uploaded_transcript_in_local_background(
    *,
    lecture_id: str,
    transcript_override: str,
    prepare_slides: bool = True,
) -> None:
    close_old_connections()
    try:
        run_note_generation_from_uploaded_transcript_sync(
            lecture_id=lecture_id,
            transcript_override=transcript_override,
            prepare_slides=prepare_slides,
        )
    except PipelineError as exc:
        _mark_manual_transcript_failure(
            lecture_id=lecture_id,
            error=exc,
        )
        logger.exception("manual_transcript_note_generation_failed", extra={"lecture_id": lecture_id, "stage": exc.stage})
    except Exception as exc:  # pragma: no cover - defensive local-thread fallback
        error = PipelineError(stage="SYSTEM", message=str(exc))
        _mark_manual_transcript_failure(
            lecture_id=lecture_id,
            error=error,
        )
        logger.exception("manual_transcript_note_generation_failed", extra={"lecture_id": lecture_id, "stage": error.stage})
    finally:
        close_old_connections()


def launch_note_generation(*, lecture_id: str, transcript_override: str | None = None) -> None:
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        worker = Thread(
            target=_run_note_generation_in_local_background,
            kwargs={"lecture_id": lecture_id, "transcript_override": transcript_override},
            name=f"tesanote-note-generation-{lecture_id}",
            daemon=True,
        )
        worker.start()
        return

    generate_note_task.delay(lecture_id, transcript_override)


def launch_note_generation_from_uploaded_transcript(
    *,
    lecture_id: str,
    transcript_override: str,
    prepare_slides: bool = True,
) -> None:
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        worker = Thread(
            target=_run_note_generation_from_uploaded_transcript_in_local_background,
            kwargs={
                "lecture_id": lecture_id,
                "transcript_override": transcript_override,
                "prepare_slides": prepare_slides,
            },
            name=f"tesanote-manual-note-generation-{lecture_id}",
            daemon=True,
        )
        worker.start()
        return

    generate_note_from_uploaded_transcript_task.delay(lecture_id, transcript_override, prepare_slides)


def _mark_manual_transcript_waiting_for_slides(*, lecture: Lecture) -> None:
    orchestrator = NoteGenerationOrchestrator(models=selected_note_models_for_lecture(lecture=lecture))
    for model_name in orchestrator.models:
        note = orchestrator.get_or_create_note(lecture=lecture, model_name=model_name)
        orchestrator.update(
            note=note,
            status=note.status or "QUEUED",
            progress=max(note.progress, 5),
            stage_detail="WAITING_FOR_SLIDES",
            stage_message="Slides are being prepared for manual transcript note generation.",
        )


def _mark_manual_transcript_failure(*, lecture_id: str, error: PipelineError) -> None:
    lecture = Lecture.objects.get(id=lecture_id)
    if lecture.slide_processing_status == SlideProcessingStatus.PROCESSING:
        SlideProcessingService().fail_pipeline(lecture=lecture, error=error)
    orchestrator = NoteGenerationOrchestrator(models=selected_note_models_for_lecture(lecture=lecture))
    for model_name in orchestrator.models:
        orchestrator.fail(lecture=lecture, model_name=model_name, error=error)


@shared_task(ignore_result=True)
def compute_note_review_task(note_id: str) -> dict[str, object]:
    """Accuracy check for one note (gated per user). Never raises into note generation."""
    try:
        from services.note_review.service import compute_note_review

        review = compute_note_review(note_id)
    except Exception:
        logger.exception("note_review_compute_failed", extra={"note_id": note_id})
        return {"note_id": note_id, "state": "ERROR"}
    if review is None:
        return {"note_id": note_id, "state": "SKIPPED"}
    return {"note_id": note_id, "state": review.state, "flagged": review.flagged_count}


@shared_task(name="apps.notes.tasks.purge_deleted_notes")
def purge_deleted_notes_task() -> dict[str, int]:
    """Hourly: remove notes the user deleted at least a day ago, from the database and storage."""
    from apps.notes.lifecycle import purge_deleted_notes

    return purge_deleted_notes()
