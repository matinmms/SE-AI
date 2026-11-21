from __future__ import annotations

import logging
import math
import mimetypes
import wave
import zipfile
from dataclasses import dataclass

import olefile
from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from mutagen import File as MutagenFile
from PyPDF2 import PdfReader

from apps.lectures.models import Lecture, LectureStatus
from apps.lectures.models import LectureProcessingState
from apps.notes.themes import normalize_note_theme
from apps.storage_lifecycle.paths import temporary_artifact_path
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from apps.subscriptions.models import PlanKey
from apps.uploads.exceptions import (
    CorruptedFileException,
    InvalidDurationException,
    InvalidFileFormatException,
    SubscriptionLimitExceededException,
    WalletInsufficientException,
)
from apps.uploads.duration import probe_audio_duration_seconds
from apps.uploads.models import LectureFile, LectureFileType, PendingSlidesUpload, UploadStatus
from apps.uploads.selectors import UploadSelector
from apps.uploads.validators import AudioFileValidator, BaseValidationResult, SlidesFileValidator
from infrastructure.storage.factory import get_storage_service
from services.analytics import perf
from services.subscriptions.lifecycle import SubscriptionLifecycleService
from services.subscriptions.quota import SubscriptionQuotaService
from services.usage.lifecycle import UsageLogService

logger = logging.getLogger("tesanote")


@dataclass(frozen=True)
class SubscriptionMinutesCheckResult:
    allowed: bool
    available_minutes: int
    required_minutes: int
    message: str
    # >0 when this is a new user's first note and the file is longer than their balance:
    # the difference is credited as bonus minutes at admission so the whole file is processed.
    first_note_grant_minutes: int = 0
    # Wallet users: the approximate price of this file and what their wallet holds.
    wallet_required: bool = False
    wallet_estimate_toman: int = 0
    wallet_available_toman: int = 0


@dataclass(frozen=True)
class ExtractedMetadata:
    mime_type: str
    file_size: int
    duration_seconds: int | None = None
    codec: str | None = None
    bitrate: int | None = None
    page_count: int | None = None

    def to_record_metadata(self) -> dict[str, int | str]:
        payload: dict[str, int | str] = {
            "file_size": self.file_size,
            "mime_type": self.mime_type,
        }
        if self.duration_seconds is not None:
            payload["duration_seconds"] = self.duration_seconds
            payload["duration_formatted"] = format_duration(self.duration_seconds)
        if self.codec is not None:
            payload["codec"] = self.codec
        if self.bitrate is not None:
            payload["bitrate"] = self.bitrate
        if self.page_count is not None:
            payload["page_count"] = self.page_count
        return payload


class UploadService:
    @staticmethod
    def list_files(*, user):
        return UploadSelector.list_files(user=user)

    @staticmethod
    def get_lecture_upload_status(*, lecture: Lecture) -> dict[str, object]:
        files = UploadSelector.list_files_for_lecture(user=lecture.user, lecture_id=lecture.id)
        return {
            "success": True,
            "lecture_id": lecture.id,
            "lecture_status": lecture.status,
            "processing_progress": lecture.processing_progress,
            "input_language": lecture.input_language,
            "output_language": lecture.output_language,
            "note_detail_mode": lecture.note_detail_mode,
            "files": [serialize_upload_file(file_obj=file_obj) for file_obj in files],
        }


def format_duration(duration_seconds: int) -> str:
    minutes, seconds = divmod(duration_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def get_upload_progress(*, upload_status: str) -> int:
    progress_map = {
        UploadStatus.PENDING: 0,
        UploadStatus.UPLOADING: 50,
        UploadStatus.UPLOADED: 100,
        UploadStatus.FAILED: 0,
    }
    return progress_map.get(upload_status, 0)


def serialize_upload_file(*, file_obj: LectureFile) -> dict[str, object]:
    return {
        "file_id": file_obj.id,
        "file_type": file_obj.file_type,
        "file_name": file_obj.original_name,
        "status": file_obj.upload_status,
        "upload_progress": get_upload_progress(upload_status=file_obj.upload_status),
        "metadata": file_obj.metadata.copy(),
        }


class SubscriptionValidationService:
    @staticmethod
    def first_note_full_applies(*, subscription, remaining_minutes: int, user=None) -> bool:
        """A new user's first note is made from the whole file, however long (up to the audio limit).

        Only a free user who still has their free minutes (a returning deleted number is given 0 on
        purpose and stays blocked), nothing used yet and nothing held by a job in flight.
        """
        if subscription is None or not settings.FIRST_NOTE_FULL_ENABLED:
            return False
        # Rolled out together with the wallet: only for the users it is switched on for.
        from apps.wallet.services import wallet_active_for

        if user is None or not wallet_active_for(user):
            return False
        plan = getattr(subscription, "plan", None)
        if plan is None or getattr(plan, "key", "") != PlanKey.FREE:
            return False
        if int(getattr(subscription, "used_minutes", 0) or 0) or int(getattr(subscription, "reserved_minutes", 0) or 0):
            return False
        return remaining_minutes >= max(1, int(settings.FIRST_NOTE_FULL_MIN_BALANCE_MINUTES))

    @staticmethod
    def remaining_minutes_check(
        *, remaining_minutes: int, duration_seconds: int, subscription=None, user=None
    ) -> SubscriptionMinutesCheckResult:
        required_minutes = SubscriptionQuotaService.minutes_required_for_duration(duration_seconds=duration_seconds)
        allowed = remaining_minutes >= required_minutes
        from apps.wallet import pricing as wallet_pricing
        from apps.wallet import services as wallet_services

        if (
            user is not None
            and wallet_services.wallet_active_for(user)
            and not SubscriptionValidationService.first_note_full_applies(
                subscription=subscription, remaining_minutes=remaining_minutes, user=user
            )
        ):
            # Wallet billing: the wallet pays, minutes are only plumbing (topped up at admission).
            estimate = wallet_pricing.estimate_for_minutes(required_minutes)
            wallet_available = wallet_services.available_toman(user)
            if wallet_available >= estimate:
                return SubscriptionMinutesCheckResult(
                    allowed=True,
                    available_minutes=max(0, remaining_minutes),
                    required_minutes=required_minutes,
                    message="آپلود مجاز است.",
                    first_note_grant_minutes=max(0, required_minutes - max(0, remaining_minutes)),
                    wallet_required=True,
                    wallet_estimate_toman=estimate,
                    wallet_available_toman=wallet_available,
                )
            return SubscriptionMinutesCheckResult(
                allowed=False,
                available_minutes=max(0, remaining_minutes),
                required_minutes=required_minutes,
                message="موجودی حسابت برای این جلسه کافی نیست؛ حساب را شارژ کن.",
                wallet_required=True,
                wallet_estimate_toman=estimate,
                wallet_available_toman=wallet_available,
            )
        if not allowed and SubscriptionValidationService.first_note_full_applies(
            subscription=subscription, remaining_minutes=remaining_minutes, user=user
        ):
            return SubscriptionMinutesCheckResult(
                allowed=True,
                available_minutes=max(0, remaining_minutes),
                required_minutes=required_minutes,
                message="اولین جزوه‌ات رایگان است.",
                first_note_grant_minutes=required_minutes - max(0, remaining_minutes),
            )
        return SubscriptionMinutesCheckResult(
            allowed=allowed,
            available_minutes=max(0, remaining_minutes),
            required_minutes=required_minutes,
            message=(
                "آپلود مجاز است."
                if allowed
                else (
                    f"دقیقه کافی ندارید: مدت فایل {required_minutes} دقیقه است "
                    f"ولی {max(0, remaining_minutes)} دقیقه باقی مانده است."
                )
            ),
        )


def apply_first_note_grant(*, user, lecture: Lecture, minutes_check: SubscriptionMinutesCheckResult) -> None:
    """Admission side effects, after the upload itself succeeded:

    - credit the minutes this file is short by, so the normal reserve/settle path processes the
      whole file (a new user's first note, and the plumbing for wallet users);
    - for wallet users, set the approximate price aside as a hold for this lecture.
    """
    grant = int(minutes_check.first_note_grant_minutes or 0)
    metadata = dict(lecture.processing_metadata or {})
    if grant > 0:
        SubscriptionLifecycleService.add_bonus_minutes(user=user, minutes=grant)
        metadata["first_note_grant_minutes"] = grant
        logger.info(
            "first_note_grant_applied",
            extra={"lecture_id": str(lecture.id), "user_id": str(user.id), "grant_minutes": grant},
        )
    if minutes_check.wallet_required and minutes_check.wallet_estimate_toman:
        from apps.wallet import services as wallet_services

        hold = wallet_services.place_hold(user=user, lecture=lecture, estimate_toman=minutes_check.wallet_estimate_toman)
        metadata["wallet_estimate_toman"] = int(minutes_check.wallet_estimate_toman)
        metadata["wallet_hold_toman"] = int(hold.amount_toman)
    if metadata != (lecture.processing_metadata or {}):
        lecture.processing_metadata = metadata
        lecture.save(update_fields=["processing_metadata", "updated_at"])


class FilePathService:
    @staticmethod
    def build_upload_path(*, user_id: str, lecture_id: str, category: str, filename: str) -> str:
        return temporary_artifact_path(
            user_id=user_id,
            lecture_id=lecture_id,
            category=f"uploads/{category}",
            filename=filename,
        )


class MetadataExtractionService:
    @staticmethod
    def _ensure_seek_start(file_obj: UploadedFile) -> None:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

    @staticmethod
    def _to_persian_number(value: int) -> str:
        digits = "۰۱۲۳۴۵۶۷۸۹"
        return "".join(digits[int(ch)] for ch in str(value))

    @classmethod
    def extract_audio_metadata(
        cls, *, file_obj: UploadedFile, validation: BaseValidationResult
    ) -> ExtractedMetadata:
        cls._ensure_seek_start(file_obj)

        codec: str | None = None
        bitrate: int | None = None
        duration_seconds: int | None = None
        extension = validation.extension

        try:
            if extension == ".wav":
                with wave.open(file_obj, "rb") as wav_file:
                    frame_rate = wav_file.getframerate()
                    frame_count = wav_file.getnframes()
                    duration_seconds = int(frame_count / float(frame_rate))
                    codec = "pcm"
                    bitrate = frame_rate * wav_file.getsampwidth() * 8 * wav_file.getnchannels()
            else:
                # Mutagen picks its parser from the file name, so e.g. raw ADTS AAC saved as ".m4a"
                # yields None. That is not corruption: fall through to the ffprobe duration check.
                parsed = None
                try:
                    parsed = MutagenFile(file_obj)
                except Exception:
                    logger.info("mutagen could not parse audio; falling back to ffprobe", exc_info=True)
                codec = validation.mime_type
                if parsed is not None and parsed.info is not None:
                    duration_seconds = int(parsed.info.length)
                    codec = parsed.mime[0] if getattr(parsed, "mime", None) else validation.mime_type
                    bitrate = int(getattr(parsed.info, "bitrate", 0) or 0)
        except wave.Error as exc:
            raise CorruptedFileException(message=f"Audio file appears corrupted: {exc}.") from exc
        except CorruptedFileException:
            raise
        except Exception as exc:
            raise CorruptedFileException(message=f"Audio file appears corrupted: {exc}.") from exc
        finally:
            cls._ensure_seek_start(file_obj)

        if duration_seconds is None or duration_seconds <= 0:
            # Some recorders write a zero/absent header duration; the container itself knows the truth.
            probed = probe_audio_duration_seconds(file_obj)
            cls._ensure_seek_start(file_obj)
            if probed is not None:
                logger.info(
                    "audio duration recovered by probe header=%s probed=%s",
                    duration_seconds,
                    probed,
                )
                duration_seconds = max(1, int(math.ceil(probed)))
        if duration_seconds is None or duration_seconds <= 0:
            raise CorruptedFileException(message="Unable to determine audio duration.")
        if duration_seconds > settings.AUDIO_MAX_DURATION_SECONDS:
            max_minutes = cls._to_persian_number(settings.AUDIO_MAX_DURATION_SECONDS // 60)
            raise InvalidDurationException(
                message=f"مدت زمان فایل صوتی نباید بیشتر از {max_minutes} دقیقه باشد."
            )

        return ExtractedMetadata(
            mime_type=validation.mime_type or mimetypes.guess_type(file_obj.name)[0] or "application/octet-stream",
            file_size=file_obj.size,
            duration_seconds=duration_seconds,
            codec=codec,
            bitrate=bitrate,
        )

    @classmethod
    def update_audio_metadata(cls, *, lecture_file: LectureFile, file_obj: UploadedFile, validation: BaseValidationResult) -> ExtractedMetadata:
        metadata = cls.extract_audio_metadata(file_obj=file_obj, validation=validation)
        lecture_file.duration_seconds = metadata.duration_seconds
        lecture_file.mime_type = metadata.mime_type
        lecture_file.file_size = metadata.file_size
        lecture_file.metadata = metadata.to_record_metadata()
        return metadata

    @classmethod
    def extract_slides_metadata(
        cls, *, file_obj: UploadedFile, validation: BaseValidationResult
    ) -> ExtractedMetadata:
        cls._ensure_seek_start(file_obj)
        extension = validation.extension
        page_count: int | None = None

        try:
            if extension == ".pdf":
                reader = PdfReader(file_obj)
                page_count = len(reader.pages)
            elif extension == ".pptx":
                with zipfile.ZipFile(file_obj) as archive:
                    if "[Content_Types].xml" not in archive.namelist():
                        raise CorruptedFileException(message="Invalid PPTX file.")
            elif extension == ".ppt":
                raise InvalidFileFormatException(
                    message="فرمت PPT قدیمی پشتیبانی نمی‌شود. لطفا فایل را به PPTX یا PDF تبدیل و دوباره بارگذاری کنید."
                )
        except CorruptedFileException:
            raise
        except InvalidFileFormatException:
            raise
        except Exception as exc:
            raise CorruptedFileException(message=f"Slides file appears corrupted: {exc}.") from exc
        finally:
            cls._ensure_seek_start(file_obj)

        return ExtractedMetadata(
            mime_type=validation.mime_type or mimetypes.guess_type(file_obj.name)[0] or "application/octet-stream",
            file_size=file_obj.size,
            page_count=page_count,
        )


class AudioUploadService:
    validator = AudioFileValidator()

    @staticmethod
    def find_by_client_upload_id(*, user, client_upload_id: str) -> tuple[Lecture, LectureFile] | None:
        lecture = (
            Lecture.objects.filter(user=user, processing_metadata__client_upload_id=client_upload_id)
            .order_by("-created_at")
            .first()
        )
        if lecture is None:
            return None
        lecture_file = lecture.files.filter(file_type=LectureFileType.AUDIO).order_by("-created_at").first()
        if lecture_file is None:
            return None
        return lecture, lecture_file

    @classmethod
    @transaction.atomic
    def create_audio_upload(
        cls,
        *,
        user,
        file_obj: UploadedFile,
        lecture_title: str,
        course_name: str,
        professor_name: str,
        note_theme: str,
        session_number: str = "",
        input_language: str,
        output_language: str,
        note_detail_mode: str,
        client_upload_id: str = "",
    ) -> tuple[Lecture, LectureFile]:
        if client_upload_id:
            existing = cls.find_by_client_upload_id(user=user, client_upload_id=client_upload_id)
            if existing is not None:
                return existing
        with perf.timed("audio_upload", "validate"):
            validation = cls.validator.validate(file_obj)
        with perf.timed("audio_upload", "probe_metadata"):
            metadata = MetadataExtractionService.extract_audio_metadata(file_obj=file_obj, validation=validation)
        active_subscription = SubscriptionLifecycleService.get_active_subscription(user=user)
        minutes_check = SubscriptionValidationService.remaining_minutes_check(
            remaining_minutes=active_subscription.available_minutes,
            duration_seconds=metadata.duration_seconds or 0,
            subscription=active_subscription,
            user=user,
        )
        if not minutes_check.allowed and minutes_check.wallet_required:
            raise WalletInsufficientException(
                available_toman=minutes_check.wallet_available_toman,
                estimate_toman=minutes_check.wallet_estimate_toman,
                required_minutes=minutes_check.required_minutes,
                message=minutes_check.message,
            )
        if not minutes_check.allowed:
            raise SubscriptionLimitExceededException(
                available_minutes=minutes_check.available_minutes,
                required_minutes=minutes_check.required_minutes,
                message=minutes_check.message,
            )

        lecture = Lecture.objects.create(
            user=user,
            title=lecture_title,
            course_name=course_name,
            professor_name=professor_name,
            session_number=session_number,
            input_language=input_language,
            output_language=output_language,
            note_detail_mode=note_detail_mode,
            status=LectureStatus.UPLOADED,
            processing_state=LectureProcessingState.UPLOADED,
            duration_seconds=metadata.duration_seconds,
            processing_progress=0,
            processing_metadata={
                **({"client_upload_id": client_upload_id} if client_upload_id else {}),
                "note_theme": normalize_note_theme(note_theme),
                "input_language": input_language,
                "output_language": output_language,
                "note_detail_mode": note_detail_mode,
                "summary_mode_enabled": note_detail_mode == "summarized",
            },
        )

        relative_path = FilePathService.build_upload_path(
            user_id=str(user.id),
            lecture_id=str(lecture.id),
            category="audio",
            filename=file_obj.name,
        )
        with perf.timed("audio_upload", "store_file"):
            stored_file = get_storage_service().save(relative_path=relative_path, file_obj=file_obj)

        lecture_file = LectureFile.objects.create(
            lecture=lecture,
            file_type=LectureFileType.AUDIO,
            original_name=file_obj.name,
            storage_path=stored_file.path,
            mime_type=metadata.mime_type,
            file_size=stored_file.size,
            duration_seconds=metadata.duration_seconds,
            upload_status=UploadStatus.UPLOADED,
            metadata=metadata.to_record_metadata(),
        )
        StorageArtifactLifecycleService.register_temporary(
            user_id=str(user.id),
            lecture_id=str(lecture.id),
            storage_path=stored_file.path,
            size_bytes=stored_file.size,
            artifact_type="source_media_upload",
        )

        logger.info(
            "audio_upload_created",
            extra={"lecture_id": str(lecture.id), "file_id": str(lecture_file.id), "path": stored_file.path},
        )
        apply_first_note_grant(user=user, lecture=lecture, minutes_check=minutes_check)
        UsageLogService.create_pending_log(
            user=user,
            lecture=lecture,
            audio_minutes=minutes_check.required_minutes,
            user_subscription=active_subscription,
        )
        return lecture, lecture_file


class SlidesUploadService:
    validator = SlidesFileValidator()

    @classmethod
    @transaction.atomic
    def attach_slides_upload(cls, *, file_obj: UploadedFile, lecture: Lecture) -> LectureFile:
        validation = cls.validator.validate(file_obj)
        metadata = MetadataExtractionService.extract_slides_metadata(file_obj=file_obj, validation=validation)

        relative_path = FilePathService.build_upload_path(
            user_id=str(lecture.user_id),
            lecture_id=str(lecture.id),
            category="slides",
            filename=file_obj.name,
        )
        stored_file = get_storage_service().save(relative_path=relative_path, file_obj=file_obj)

        lecture_file = LectureFile.objects.create(
            lecture=lecture,
            file_type=LectureFileType.SLIDES,
            original_name=file_obj.name,
            storage_path=stored_file.path,
            mime_type=metadata.mime_type,
            file_size=stored_file.size,
            duration_seconds=None,
            upload_status=UploadStatus.UPLOADED,
            metadata=metadata.to_record_metadata(),
        )
        StorageArtifactLifecycleService.register_temporary(
            user_id=str(lecture.user_id),
            lecture_id=str(lecture.id),
            storage_path=stored_file.path,
            size_bytes=stored_file.size,
            artifact_type="source_slides_upload",
        )

        logger.info(
            "slides_upload_created",
            extra={"lecture_id": str(lecture.id), "file_id": str(lecture_file.id), "path": stored_file.path},
        )
        return lecture_file

    @classmethod
    def upload_for_client_upload(cls, *, file_obj: UploadedFile, user, client_upload_id: str):
        """Slides sent alongside an audio upload that may not be finalized yet.

        Returns (lecture, LectureFile) when the lecture exists (now or by the time the slides are
        stored), else (None, PendingSlidesUpload) to be attached when the audio finalizes.
        """
        found = AudioUploadService.find_by_client_upload_id(user=user, client_upload_id=client_upload_id)
        if found is not None:
            return found[0], cls.attach_slides_upload(file_obj=file_obj, lecture=found[0])

        validation = cls.validator.validate(file_obj)
        metadata = MetadataExtractionService.extract_slides_metadata(file_obj=file_obj, validation=validation)
        relative_path = FilePathService.build_upload_path(
            user_id=str(user.id),
            lecture_id=f"pending-{client_upload_id}",
            category="slides",
            filename=file_obj.name,
        )
        stored_file = get_storage_service().save(relative_path=relative_path, file_obj=file_obj)
        StorageArtifactLifecycleService.register_temporary(
            user_id=str(user.id),
            lecture_id=None,
            storage_path=stored_file.path,
            size_bytes=stored_file.size,
            artifact_type="source_slides_upload",
        )
        pending, _ = PendingSlidesUpload.objects.update_or_create(
            user=user,
            client_upload_id=client_upload_id,
            defaults={
                "original_name": file_obj.name,
                "storage_path": stored_file.path,
                "mime_type": metadata.mime_type or "",
                "file_size": stored_file.size,
                "metadata": metadata.to_record_metadata(),
            },
        )
        logger.info("slides_upload_pending", extra={"client_upload_id": client_upload_id, "path": stored_file.path})
        # The audio may have finalized while these slides were being stored; it would not have
        # seen them, so attach now. (Each side checks for the other after committing its own row.)
        found = AudioUploadService.find_by_client_upload_id(user=user, client_upload_id=client_upload_id)
        if found is not None:
            attached = cls.claim_pending_slides(lecture=found[0])
            if attached is not None:
                return found[0], attached
        return None, pending

    @classmethod
    def claim_pending_slides(cls, *, lecture: Lecture) -> LectureFile | None:
        """Attach slides that were uploaded before this lecture existed. Safe to call from both
        sides at once: the row is locked and deleted, so the slides are attached exactly once."""
        client_upload_id = str((lecture.processing_metadata or {}).get("client_upload_id") or "")
        if not client_upload_id:
            return None
        with transaction.atomic():
            pending = (
                PendingSlidesUpload.objects.select_for_update()
                .filter(user_id=lecture.user_id, client_upload_id=client_upload_id)
                .first()
            )
            if pending is None:
                return None
            existing = lecture.files.filter(file_type=LectureFileType.SLIDES).order_by("-created_at").first()
            if existing is not None:
                pending.delete()
                return existing
            lecture_file = LectureFile.objects.create(
                lecture=lecture,
                file_type=LectureFileType.SLIDES,
                original_name=pending.original_name,
                storage_path=pending.storage_path,
                mime_type=pending.mime_type,
                file_size=pending.file_size,
                duration_seconds=None,
                upload_status=UploadStatus.UPLOADED,
                metadata=pending.metadata,
            )
            pending.delete()
        logger.info("slides_upload_attached", extra={"lecture_id": str(lecture.id), "file_id": str(lecture_file.id)})
        return lecture_file
