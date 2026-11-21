from __future__ import annotations

import logging

from django.utils import timezone

from apps.lectures.models import Lecture, LectureProcessingState, LectureStatus


class LectureProcessingStateService:
    @staticmethod
    def set_state(
        *,
        lecture: Lecture,
        state: str,
        progress: int | None = None,
        error_message: str = "",
        status: str | None = None,
    ) -> None:
        lecture.processing_state = state
        if progress is not None:
            lecture.processing_progress = progress
        if status is not None:
            lecture.status = status
        if state == LectureProcessingState.QUEUED:
            lecture.processing_started_at = timezone.now()
            lecture.processing_completed_at = None
            lecture.processing_error = ""
        elif state == LectureProcessingState.COMPLETED:
            lecture.processing_completed_at = timezone.now()
            lecture.processing_error = ""
        elif state == LectureProcessingState.FAILED:
            lecture.processing_completed_at = timezone.now()
            lecture.processing_error = error_message
        elif error_message or error_message == "":
            lecture.processing_error = error_message

        lecture.save(
            update_fields=[
                "processing_state",
                "processing_progress",
                "processing_started_at",
                "processing_completed_at",
                "processing_error",
                "status",
                "updated_at",
            ]
        )
        if state == LectureProcessingState.FAILED:
            # Legacy lectures do not have a job record, so this is safely a no-op
            # until a processing attempt has been durably accepted.
            from apps.ai_pipeline.jobs import ProcessingJobService

            ProcessingJobService.mark_failed_for_lecture(
                lecture_id=str(lecture.id),
                stage="LECTURE_PROCESSING_FAILED",
            )

    @staticmethod
    def mark_uploaded(*, lecture: Lecture) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.UPLOADED,
            progress=0,
            status=LectureStatus.UPLOADED,
        )

    @staticmethod
    def mark_queued(*, lecture: Lecture) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.QUEUED,
            progress=max(lecture.processing_progress, 0),
            error_message="",
            status=LectureStatus.PROCESSING,
        )

    @staticmethod
    def mark_transcribing(*, lecture: Lecture) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.TRANSCRIBING,
            progress=max(lecture.processing_progress, 1),
            status=LectureStatus.PROCESSING,
        )

    @staticmethod
    def mark_analyzing_slides(*, lecture: Lecture, progress: int | None = None) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.ANALYZING_SLIDES,
            progress=progress if progress is not None else max(lecture.processing_progress, 1),
            status=LectureStatus.PROCESSING,
        )

    @staticmethod
    def mark_generating_notes(*, lecture: Lecture, progress: int | None = None) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.GENERATING_NOTES,
            progress=progress if progress is not None else max(lecture.processing_progress, 1),
            status=LectureStatus.PROCESSING,
        )

    @staticmethod
    def mark_generating_pdf(*, lecture: Lecture, progress: int | None = None) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.GENERATING_PDF,
            progress=progress if progress is not None else max(lecture.processing_progress, 1),
            status=LectureStatus.PROCESSING,
        )

    @staticmethod
    def mark_completed(*, lecture: Lecture) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.COMPLETED,
            progress=100,
            status=LectureStatus.COMPLETED,
        )
        try:
            from services.analytics.product import ProductAnalyticsService

            ProductAnalyticsService.record_first_note_ready(user=lecture.user)
        except Exception:
            logging.getLogger(__name__).exception("first_note_ready_event_failed")
        try:
            from services.payments.first_purchase import FirstPurchaseDiscountService

            FirstPurchaseDiscountService.open_window(user=lecture.user, first_note_ready=True)
        except Exception:
            logging.getLogger(__name__).exception("first_purchase_discount_open_failed")
        try:
            from apps.referrals.services import record_activation

            record_activation(user=lecture.user)
        except Exception:
            logging.getLogger(__name__).exception("share_activation_event_failed")

    @staticmethod
    def mark_failed(*, lecture: Lecture, error_message: str) -> None:
        LectureProcessingStateService.set_state(
            lecture=lecture,
            state=LectureProcessingState.FAILED,
            progress=max(lecture.processing_progress, 1),
            error_message=error_message,
            status=LectureStatus.FAILED,
        )
        # Several independent stage-level failure handlers (audio
        # preprocessing, per-chunk transcription, note generation, the
        # top-level orchestrator) all funnel a terminal failure through
        # this one shared setter, but had each grown their own ad hoc
        # "mark the lecture failed" logic without consistently also
        # releasing the minutes reserved for this upload -- so a failed
        # upload could leave "available minutes" (remaining - reserved)
        # permanently reduced even though the raw remaining-minutes
        # balance was correctly left untouched. Releasing here, once, for
        # every caller is more robust than hunting down and patching each
        # call site individually. fail_processing_once() is idempotent
        # (it no-ops once the usage log is no longer PENDING/PROCESSING),
        # so this is safe even for a caller that also already releases it
        # explicitly.
        from services.usage.lifecycle import UsageLogService

        usage_log = UsageLogService.get_latest_for_lecture(lecture=lecture)
        if usage_log is not None:
            UsageLogService.fail_processing_once(usage_log=usage_log, error_message=error_message)
