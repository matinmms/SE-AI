from __future__ import annotations

import logging

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.usage.models import UsageLog, UsageLogStatus
from services.costs import CostCalculatorService
from services.subscriptions.lifecycle import SubscriptionLifecycleService

logger = logging.getLogger("tesanote")


class UsageLogService:
    @staticmethod
    def _transcription_metadata_for_usage(*, usage_log: UsageLog) -> dict[str, object]:
        lecture = usage_log.lecture
        transcript_metadata = lecture.transcript_metadata or {}
        return {
            "transcription_provider_used": str(
                transcript_metadata.get("transcription_provider_used")
                or lecture.transcript_provider
                or ""
            ),
            "transcription_model_used": str(transcript_metadata.get("transcription_model_used") or ""),
            "transcription_fallback_used": bool(transcript_metadata.get("fallback_used", False)),
            "transcription_chunks_count": int(transcript_metadata.get("transcription_chunks_count", 0) or 0),
            "transcription_latency_ms": int(transcript_metadata.get("transcription_latency_ms", 0) or 0),
            "transcription_audio_seconds": float(
                transcript_metadata.get("transcription_audio_seconds")
                or lecture.transcript_duration_processed
                or 0
            ),
            "transcription_audio_size_mb": float(transcript_metadata.get("transcription_audio_size_mb", 0.0) or 0.0),
            "transcription_error_message": str(
                transcript_metadata.get("error_message")
                or lecture.processing_error
                or ""
            ),
        }

    @staticmethod
    def get_latest_for_lecture(*, lecture) -> UsageLog | None:
        return lecture.usage_logs.order_by("-created_at").first()

    @staticmethod
    def create_pending_log(
        *, user, lecture, audio_minutes: int, user_subscription=None, minutes_reserved: bool = False
    ) -> UsageLog:
        subscription = user_subscription or SubscriptionLifecycleService.get_active_subscription(user=user)
        return UsageLog.objects.create(
            user=user,
            lecture=lecture,
            user_subscription=subscription,
            audio_minutes=audio_minutes,
            status=UsageLogStatus.PENDING,
            minutes_reserved=minutes_reserved,
        )

    @staticmethod
    @transaction.atomic
    def reserve_minutes_once(*, usage_log: UsageLog, enforce_available: bool) -> UsageLog:
        """Hold this log's minutes on the subscription, at most once.

        Called when processing is accepted, never at upload time, so an
        abandoned or failed upload cannot lock up the user's balance.
        Raises ValueError("INSUFFICIENT_AVAILABLE_MINUTES") when enforcing.
        """
        from services.subscriptions.quota import SubscriptionQuotaService

        locked_log = UsageLog.objects.select_for_update().select_related("user", "lecture").get(id=usage_log.id)
        if locked_log.minutes_reserved or locked_log.status in {UsageLogStatus.COMPLETED}:
            usage_log.minutes_reserved = locked_log.minutes_reserved
            return usage_log
        SubscriptionQuotaService.reserve_minutes(
            user=locked_log.user,
            minutes=locked_log.audio_minutes,
            lecture=locked_log.lecture,
            enforce_available=enforce_available,
        )
        locked_log.minutes_reserved = True
        locked_log.save(update_fields=["minutes_reserved", "updated_at"])
        usage_log.minutes_reserved = True
        return usage_log

    @staticmethod
    def mark_processing(*, usage_log: UsageLog) -> UsageLog:
        # Admission already happened when the job was accepted; this only covers logs
        # that reach the pipeline without a hold (older rows, scheduler-started jobs).
        try:
            UsageLogService.reserve_minutes_once(usage_log=usage_log, enforce_available=False)
        except ObjectDoesNotExist:
            logger.warning("usage_reserve_skipped_no_active_subscription", extra={"usage_log_id": str(usage_log.id)})
        usage_log.status = UsageLogStatus.PROCESSING
        usage_log.started_at = usage_log.started_at or timezone.now()
        usage_log.error_message = ""
        usage_log.save(update_fields=["status", "started_at", "error_message", "updated_at"])
        return usage_log

    @staticmethod
    def _resolve_actual_duration_seconds(*, usage_log: UsageLog) -> float:
        """The true, pipeline-measured length of the user's uploaded audio.

        Prefers `original_duration` (the raw pre-optimization duration the
        audio-preprocessing stage measures directly from the file, before
        silence-removal/speed-up) because upload-time admission (the 90-
        minute cap, the initial quota reservation) is itself defined in
        terms of that same raw duration. Falls back to the transcription
        stage's own measured duration (the post-optimization audio actually
        sent to the provider) only if the original was never recorded, and
        finally to 0 (meaning: no correction possible) if neither exists.
        """
        lecture = usage_log.lecture
        metadata = lecture.processing_metadata or {}
        original_duration = metadata.get("original_duration")
        if original_duration:
            try:
                return float(original_duration)
            except (TypeError, ValueError):
                pass
        transcript_metadata = lecture.transcript_metadata or {}
        measured = (
            transcript_metadata.get("transcription_audio_seconds")
            or lecture.transcript_duration_processed
        )
        if measured:
            try:
                return float(measured)
            except (TypeError, ValueError):
                pass
        return 0.0

    @staticmethod
    def _resolve_actual_audio_minutes(*, usage_log: UsageLog) -> int:
        """The minutes that should actually be billed for a finished
        lecture -- corrects for any upload-time duration-estimation error
        (e.g. a metadata-extraction failure that under-counted the file as
        1 minute) using the real duration the pipeline has since measured.
        Never used for admission; only for the one-time finalize deduction.
        """
        actual_seconds = UsageLogService._resolve_actual_duration_seconds(usage_log=usage_log)
        if actual_seconds <= 0:
            return usage_log.audio_minutes
        from services.subscriptions.quota import SubscriptionQuotaService

        return SubscriptionQuotaService.minutes_required_for_duration(
            duration_seconds=actual_seconds
        )

    @staticmethod
    def mark_completed(*, usage_log: UsageLog, audio_minutes: int | None = None) -> UsageLog:
        if audio_minutes is not None:
            usage_log.audio_minutes = audio_minutes
        breakdown = CostCalculatorService.estimate_for_audio_minutes(audio_minutes=usage_log.audio_minutes)
        transcription_metadata = UsageLogService._transcription_metadata_for_usage(usage_log=usage_log)
        usage_log.status = UsageLogStatus.COMPLETED
        usage_log.started_at = usage_log.started_at or timezone.now()
        usage_log.completed_at = timezone.now()
        usage_log.avenger_cost = breakdown.avenger_cost
        usage_log.llm_cost = breakdown.llm_cost
        usage_log.pdf_cost = breakdown.pdf_cost
        usage_log.storage_cost = breakdown.storage_cost
        usage_log.total_cost = breakdown.total_cost
        usage_log.error_message = ""
        usage_log.transcription_provider_used = str(transcription_metadata["transcription_provider_used"])
        usage_log.transcription_model_used = str(transcription_metadata["transcription_model_used"])
        usage_log.transcription_fallback_used = bool(transcription_metadata["transcription_fallback_used"])
        usage_log.transcription_chunks_count = int(transcription_metadata["transcription_chunks_count"])
        usage_log.transcription_latency_ms = int(transcription_metadata["transcription_latency_ms"])
        usage_log.transcription_audio_seconds = float(transcription_metadata["transcription_audio_seconds"])
        usage_log.transcription_audio_size_mb = float(transcription_metadata["transcription_audio_size_mb"])
        usage_log.transcription_error_message = ""
        usage_log.save(
            update_fields=[
                "audio_minutes",
                "status",
                "started_at",
                "completed_at",
                "avenger_cost",
                "llm_cost",
                "pdf_cost",
                "storage_cost",
                "total_cost",
                "error_message",
                "transcription_provider_used",
                "transcription_model_used",
                "transcription_fallback_used",
                "transcription_chunks_count",
                "transcription_latency_ms",
                "transcription_audio_seconds",
                "transcription_audio_size_mb",
                "transcription_error_message",
                "updated_at",
            ]
        )
        return usage_log

    @staticmethod
    def mark_failed(*, usage_log: UsageLog, error_message: str) -> UsageLog:
        transcription_metadata = UsageLogService._transcription_metadata_for_usage(usage_log=usage_log)
        usage_log.status = UsageLogStatus.FAILED
        usage_log.started_at = usage_log.started_at or timezone.now()
        usage_log.completed_at = timezone.now()
        usage_log.error_message = error_message
        usage_log.transcription_provider_used = str(transcription_metadata["transcription_provider_used"])
        usage_log.transcription_model_used = str(transcription_metadata["transcription_model_used"])
        usage_log.transcription_fallback_used = bool(transcription_metadata["transcription_fallback_used"])
        usage_log.transcription_chunks_count = int(transcription_metadata["transcription_chunks_count"])
        usage_log.transcription_latency_ms = int(transcription_metadata["transcription_latency_ms"])
        usage_log.transcription_audio_seconds = float(transcription_metadata["transcription_audio_seconds"])
        usage_log.transcription_audio_size_mb = float(transcription_metadata["transcription_audio_size_mb"])
        usage_log.transcription_error_message = str(
            transcription_metadata["transcription_error_message"] or error_message
        )
        usage_log.save(
            update_fields=[
                "status",
                "started_at",
                "completed_at",
                "error_message",
                "transcription_provider_used",
                "transcription_model_used",
                "transcription_fallback_used",
                "transcription_chunks_count",
                "transcription_latency_ms",
                "transcription_audio_seconds",
                "transcription_audio_size_mb",
                "transcription_error_message",
                "updated_at",
            ]
        )
        logger.info(
            "processing_failed",
            extra={
                "user_id": str(usage_log.user_id),
                "lecture_id": str(usage_log.lecture_id),
                "subscription_id": str(usage_log.user_subscription_id or ""),
                "usage_log_id": str(usage_log.id),
            },
        )
        return usage_log

    @staticmethod
    @transaction.atomic
    def finalize_processing_once(*, usage_log: UsageLog) -> bool:
        """Finalize one reservation at most once, even if a task is retried."""
        locked_log = UsageLog.objects.select_for_update().select_related("lecture", "user").get(id=usage_log.id)
        if locked_log.status == UsageLogStatus.COMPLETED:
            return False
        if locked_log.status not in {UsageLogStatus.PENDING, UsageLogStatus.PROCESSING}:
            return False

        reserved_minutes = locked_log.audio_minutes if locked_log.minutes_reserved else 0
        actual_minutes = UsageLogService._resolve_actual_audio_minutes(usage_log=locked_log)
        UsageLogService.mark_completed(usage_log=locked_log, audio_minutes=actual_minutes)
        locked_log.minutes_reserved = False
        locked_log.save(update_fields=["minutes_reserved", "updated_at"])
        from services.subscriptions.quota import SubscriptionQuotaService

        SubscriptionQuotaService.finalize_actual_minutes(
            user=locked_log.user,
            reserved_minutes=reserved_minutes,
            actual_minutes=actual_minutes,
            lecture=locked_log.lecture,
        )
        UsageLogService._settle_wallet(lecture=locked_log.lecture, minutes=actual_minutes)
        return True

    @staticmethod
    def _settle_wallet(*, lecture, minutes: int) -> None:
        """Wallet users pay the note's real cost plus the per-minute profit once it is done.

        A wallet problem must never break note delivery: it is logged and the hold stays, so the
        `wallet_settle_stuck_holds` command can finish it."""
        try:
            from apps.wallet import services as wallet_services

            wallet_services.settle_hold(lecture=lecture, minutes=minutes)
        except Exception:
            logger.exception("wallet_settle_failed", extra={"lecture_id": str(lecture.id)})

    @staticmethod
    @transaction.atomic
    def fail_processing_once(*, usage_log: UsageLog, error_message: str) -> bool:
        """Release the usage reservation only for the attempt that owns it."""
        locked_log = UsageLog.objects.select_for_update().select_related("lecture", "user").get(id=usage_log.id)
        if locked_log.status not in {UsageLogStatus.PENDING, UsageLogStatus.PROCESSING}:
            return False

        UsageLogService.mark_failed(usage_log=locked_log, error_message=error_message)
        if locked_log.minutes_reserved:
            from services.subscriptions.quota import SubscriptionQuotaService

            SubscriptionQuotaService.release_reserved_minutes(
                user=locked_log.user,
                minutes=locked_log.audio_minutes,
                lecture=locked_log.lecture,
            )
            locked_log.minutes_reserved = False
            locked_log.save(update_fields=["minutes_reserved", "updated_at"])
        try:
            from apps.wallet import services as wallet_services

            wallet_services.release_hold(lecture=locked_log.lecture)
        except Exception:
            logger.exception("wallet_release_failed", extra={"lecture_id": str(locked_log.lecture_id)})
        return True

    @staticmethod
    def list_user_usage(*, user):
        return UsageLog.objects.filter(user=user).select_related("lecture").order_by("-created_at")[:50]

    @staticmethod
    def build_user_usage_summary(*, user) -> dict[str, int]:
        aggregates = UsageLog.objects.filter(user=user, status=UsageLogStatus.COMPLETED).aggregate(
            total_audio_minutes=Sum("audio_minutes"),
            total_cost=Sum("total_cost"),
        )
        return {
            "total_audio_minutes": int(aggregates["total_audio_minutes"] or 0),
            "total_cost": int(aggregates["total_cost"] or 0),
        }
