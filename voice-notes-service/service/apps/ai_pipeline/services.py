from __future__ import annotations

import logging
from datetime import datetime
from datetime import timezone as dt_timezone
from threading import Thread
from uuid import uuid4

from celery.result import AsyncResult
from django.conf import settings
from django.db import close_old_connections, transaction
from django.utils import timezone
from kombu.exceptions import OperationalError
from redis.exceptions import RedisError

from apps.ai_pipeline.exceptions import PipelineError
from apps.ai_pipeline.extractors.formulas import FormulaExtractor
from apps.ai_pipeline.heartbeat import HeartbeatService
from apps.ai_pipeline.models import LectureProcessingJobStatus
from apps.ai_pipeline.extractors.ocr import OCRExtractor
from apps.ai_pipeline.extractors.tables import TableExtractor
from apps.ai_pipeline.extractors.thumbnails import ThumbnailGenerator
from apps.ai_pipeline.jobs import ProcessingJobService
from apps.ai_pipeline.parsers.pdf import PDFSlideParser
from apps.ai_pipeline.parsers.pptx import PPTXSlideParser
from apps.ai_pipeline.timeouts import enforce_lecture_processing_timeouts
from apps.ai_pipeline.types import ParsedSlidesDocument
from apps.audio_pipeline.providers.router_provider import RoutedTranscriptionProvider
from apps.audio_pipeline.services import AudioPreprocessingService
from apps.audio_pipeline.transcription.service import TranscriptionOrchestratorService
from apps.lectures.models import (
    AudioProcessingStatus,
    Lecture,
    LectureStatus,
    SlidePage,
    SlideProcessingStatus,
    TranscriptionStatus,
)
from apps.notes.generation import (
    NoteGenerationOrchestrator,
    NoteGenerationReadinessService,
    aggregate_note_status,
    is_comparison_note_variant,
    normalize_note_variant,
    selected_note_models_for_lecture,
)
from apps.notes.models import GeneratedNote, NoteGenerationStatus
from apps.uploads.models import LectureFile, LectureFileType, UploadStatus
from infrastructure.observability.scaling import measure_scaling_stage
from infrastructure.storage.factory import get_storage_service
from services.errors.recorder import ErrorRecordingService
from services.lectures.processing import LectureProcessingStateService
from services.usage.lifecycle import UsageLogService

logger = logging.getLogger("celery")

WAITING_FOR_SLIDES = "WAITING_FOR_SLIDES"


class AIPipelineService:
    @staticmethod
    def get_status() -> dict[str, str]:
        return {"status": "available"}


class LectureProcessingOrchestrator:
    def __init__(self) -> None:
        self.slide_service = SlideProcessingService()
        self.audio_service = AudioPreprocessingService()
        self.transcription_service: TranscriptionOrchestratorService | None = None
        self.note_service: NoteGenerationOrchestrator | None = None
        self.note_readiness_service = NoteGenerationReadinessService()

    def validate(self, *, lecture: Lecture) -> None:
        self.audio_service.get_audio_file(lecture=lecture)

    def prepare_note_generation_models(
        self, *, lecture: Lecture, force_refresh: bool = False
    ) -> dict[str, object]:
        return self.note_readiness_service.persist_selection(
            lecture=lecture,
            preferred_models=selected_note_models_for_lecture(lecture=lecture),
            force_refresh=force_refresh,
        )

    def start(self, *, lecture: Lecture, task_id: str) -> None:
        selected_model = str(
            (lecture.processing_metadata or {}).get("note_generation_selected_model", "") or ""
        ).strip()
        if not selected_model:
            self.prepare_note_generation_models(lecture=lecture)
            lecture.refresh_from_db()
        started_at = datetime.now(dt_timezone.utc).isoformat()
        LectureProcessingStateService.mark_queued(lecture=lecture)
        lecture.status = LectureStatus.PROCESSING
        lecture.processing_progress = 0
        lecture.slide_processing_task_id = task_id
        lecture.slide_processing_error = ""
        lecture.processing_error = ""
        lecture.transcription_status = TranscriptionStatus.QUEUED
        lecture.transcription_progress = 0
        lecture.processing_metadata = {
            **lecture.processing_metadata,
            "pipeline_task_id": task_id,
            "pipeline_started_at": started_at,
        }
        lecture.save(
            update_fields=[
                "status",
                "processing_progress",
                "slide_processing_task_id",
                "slide_processing_error",
                "processing_error",
                "transcription_status",
                "transcription_progress",
                "processing_metadata",
                "updated_at",
            ]
        )
        usage_log = UsageLogService.get_latest_for_lecture(lecture=lecture)
        if usage_log is not None:
            UsageLogService.mark_processing(usage_log=usage_log)

        if lecture.files.filter(
            file_type=LectureFileType.SLIDES, upload_status=UploadStatus.UPLOADED
        ).exists():
            self.slide_service.start_processing(lecture=lecture, task_id=task_id)
        self.audio_service.start_processing(lecture=lecture)

    def run(self, *, lecture: Lecture) -> dict[str, object]:
        slide_result: dict[str, object] | None = None
        has_slides = lecture.files.filter(
            file_type=LectureFileType.SLIDES, upload_status=UploadStatus.UPLOADED
        ).exists()
        run_slides_inline = has_slides and getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)
        if has_slides and not run_slides_inline:
            # Slides are read by their own task while the audio is transcribed;
            # the note starts once both are done (see dispatch_note_generation).
            if lecture.slide_processing_status != SlideProcessingStatus.QUEUED:
                self.slide_service.start_processing(
                    lecture=lecture, task_id=lecture.slide_processing_task_id or str(uuid4())
                )
            from apps.ai_pipeline.tasks import enqueue_slide_processing

            enqueue_slide_processing(lecture_id=str(lecture.id))
            slide_result = {"status": SlideProcessingStatus.QUEUED}
        with HeartbeatService.keepalive(lecture_id=str(lecture.id)):
            audio_result = self.audio_service.run_pipeline(lecture=lecture)
            if run_slides_inline:
                slide_result = self.slide_service.run_pipeline(lecture=lecture)
        transcription_result = self.dispatch_transcription(lecture=lecture)
        lecture.refresh_from_db()
        note_result: dict[str, object] | None = None
        if (
            lecture.transcription_status == TranscriptionStatus.COMPLETED
            and lecture.merged_transcript.strip()
        ):
            note_result = self.dispatch_note_generation(lecture=lecture)
        elif lecture.transcription_status == TranscriptionStatus.COMPLETED:
            raise PipelineError(
                stage="NOTE_GENERATION",
                message="Transcript is empty; the lecture could not be finalized.",
            )
        return {
            "audio": audio_result,
            "slides": slide_result,
            "transcription": transcription_result,
            "notes": note_result,
        }

    def dispatch_transcription(self, *, lecture: Lecture) -> dict[str, object]:
        if self.transcription_service is None:
            self.transcription_service = TranscriptionOrchestratorService(
                provider=RoutedTranscriptionProvider()
            )
        self.transcription_service.start(lecture=lecture)
        chunk_ids: list[str] = []
        for chunk in lecture.audio_chunks.all().order_by("chunk_index"):
            chunk_ids.append(str(chunk.id))
        if chunk_ids:
            from apps.ai_pipeline.tasks import (
                enqueue_transcription_batch,
                ensure_transcription_started,
                run_transcription_batch_sync,
            )

            if settings.CELERY_TASK_ALWAYS_EAGER:
                run_transcription_batch_sync(
                    lecture_id=str(lecture.id),
                    start_index=None,
                    limit=settings.TRANSCRIPTION_BATCH_SIZE,
                )
            else:
                enqueue_transcription_batch(
                    lecture_id=str(lecture.id),
                    start_index=None,
                    limit=settings.TRANSCRIPTION_BATCH_SIZE,
                )
                ensure_transcription_started.apply_async(
                    args=[str(lecture.id)], countdown=20, queue="pipeline"
                )
        return {"chunk_ids": chunk_ids, "status": lecture.transcription_status}

    def dispatch_note_generation(self, *, lecture: Lecture) -> dict[str, object]:
        selected_models = selected_note_models_for_lecture(lecture=lecture)
        if self.note_service is None:
            self.note_service = NoteGenerationOrchestrator(models=selected_models)

        existing_notes = list(lecture.generated_notes.all())
        current_note_status, _ = aggregate_note_status(lecture=lecture)
        completed_variants = {
            note.variant
            for note in existing_notes
            if note.status == NoteGenerationStatus.COMPLETED and note.html_path
        }
        required_variants = {normalize_note_variant(model_name) for model_name in selected_models}
        if required_variants and completed_variants >= required_variants:
            return {"status": current_note_status}
        if current_note_status in {
            NoteGenerationStatus.QUEUED,
            NoteGenerationStatus.SUMMARIZING,
            NoteGenerationStatus.STRUCTURING,
            NoteGenerationStatus.RENDERING,
        }:
            return {"status": current_note_status}

        # Slide text is part of the note's source material. Starting while the
        # slides are still being read would produce a note without them, so
        # wait; the slide task dispatches the note when it finishes or fails.
        lecture.refresh_from_db(fields=["slide_processing_status"])
        if lecture.slide_processing_status in {
            SlideProcessingStatus.QUEUED,
            SlideProcessingStatus.PROCESSING,
        }:
            return {"status": WAITING_FOR_SLIDES}

        from apps.ai_pipeline.tasks import enqueue_note_generation
        from apps.notes.tasks import run_note_generation_sync

        if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
            return run_note_generation_sync(lecture_id=str(lecture.id))
        for model_name in self.note_service.models:
            self.note_service.start(lecture=lecture, model_name=model_name)
        enqueue_note_generation(lecture_id=str(lecture.id))
        return {"status": NoteGenerationStatus.QUEUED}

    def fail(
        self, *, lecture: Lecture, error: PipelineError, expected_generation: int | None = None
    ) -> None:
        if not ProcessingJobService.is_current_execution(
            lecture_id=str(lecture.id), expected_generation=expected_generation
        ):
            logger.warning(
                "processing_fail_skipped_fenced_attempt",
                extra={"lecture_id": str(lecture.id), "caller_generation": expected_generation},
            )
            return
        ProcessingJobService.mark_failed_for_lecture(
            lecture_id=str(lecture.id),
            stage=error.stage,
            expected_generation=expected_generation,
        )
        ErrorRecordingService.record_processing_error(
            user=lecture.user,
            lecture=lecture,
            stage=error.stage,
            error_type=error.__class__.__name__,
            error_message=error.message,
        )
        LectureProcessingStateService.mark_failed(lecture=lecture, error_message=error.message)
        lecture.status = LectureStatus.FAILED
        lecture.processing_metadata = {
            **lecture.processing_metadata,
            "pipeline_error": error.message,
        }
        if lecture.transcription_status in {
            TranscriptionStatus.QUEUED,
            TranscriptionStatus.TRANSCRIBING,
            TranscriptionStatus.CLEANING,
            TranscriptionStatus.MERGING,
        }:
            lecture.transcription_status = TranscriptionStatus.FAILED
            lecture.transcript_metadata = {
                **lecture.transcript_metadata,
                "stage": error.stage,
                "error_message": error.message,
            }
            lecture.save(
                update_fields=[
                    "status",
                    "processing_metadata",
                    "transcription_status",
                    "transcript_metadata",
                    "updated_at",
                ]
            )
        else:
            lecture.save(update_fields=["status", "processing_metadata", "updated_at"])
        if lecture.slide_processing_status not in {
            SlideProcessingStatus.COMPLETED,
            SlideProcessingStatus.FAILED,
            SlideProcessingStatus.NOT_STARTED,
        }:
            self.slide_service.fail_pipeline(lecture=lecture, error=error)
        if lecture.audio_processing_status not in {
            AudioProcessingStatus.COMPLETED,
            AudioProcessingStatus.FAILED,
            AudioProcessingStatus.NOT_STARTED,
        }:
            self.audio_service.fail_pipeline(lecture=lecture, error=error)
        usage_log = UsageLogService.get_latest_for_lecture(lecture=lecture)
        if usage_log is not None:
            UsageLogService.fail_processing_once(usage_log=usage_log, error_message=error.message)


class SlideProcessingService:
    def __init__(self) -> None:
        self.storage = get_storage_service()
        self.pdf_parser = PDFSlideParser()
        self.pptx_parser = PPTXSlideParser()
        self.ocr_extractor = OCRExtractor()
        self.table_extractor = TableExtractor()
        self.formula_extractor = FormulaExtractor()
        self.thumbnail_generator = ThumbnailGenerator()

    def get_slides_file(self, *, lecture: Lecture) -> LectureFile:
        slides_file = (
            LectureFile.objects.filter(
                lecture=lecture,
                file_type=LectureFileType.SLIDES,
                upload_status=UploadStatus.UPLOADED,
            )
            .order_by("-created_at")
            .first()
        )
        if slides_file is None:
            raise PipelineError(
                stage="VALIDATION", message="No uploaded slides file found for this lecture."
            )
        return slides_file

    def start_processing(self, *, lecture: Lecture, task_id: str) -> None:
        LectureProcessingStateService.mark_analyzing_slides(lecture=lecture, progress=0)
        lecture.status = LectureStatus.PROCESSING
        lecture.processing_progress = 0
        lecture.slide_processing_status = SlideProcessingStatus.QUEUED
        lecture.slide_processing_progress = 0
        lecture.slide_processing_stage = "QUEUED"
        lecture.slide_processing_error = ""
        lecture.slide_processing_task_id = task_id
        lecture.slide_processing_debug = {}
        lecture.slides_ready = False
        lecture.save(
            update_fields=[
                "status",
                "processing_progress",
                "slide_processing_status",
                "slide_processing_progress",
                "slide_processing_stage",
                "slide_processing_error",
                "slide_processing_task_id",
                "slide_processing_debug",
                "slides_ready",
                "updated_at",
            ]
        )

    def update_progress(
        self,
        *,
        lecture: Lecture,
        stage: str,
        progress: int,
        status: str = SlideProcessingStatus.PROCESSING,
        debug: dict[str, object] | None = None,
    ) -> None:
        LectureProcessingStateService.mark_analyzing_slides(lecture=lecture, progress=progress)
        lecture.slide_processing_status = status
        lecture.slide_processing_stage = stage
        lecture.slide_processing_progress = progress
        lecture.processing_progress = progress
        if debug is not None:
            lecture.slide_processing_debug = {**lecture.slide_processing_debug, **debug}
        lecture.save(
            update_fields=[
                "slide_processing_status",
                "slide_processing_stage",
                "slide_processing_progress",
                "processing_progress",
                "slide_processing_debug",
                "updated_at",
            ]
        )

    def fail_pipeline(self, *, lecture: Lecture, error: PipelineError) -> None:
        ErrorRecordingService.record_processing_error(
            user=lecture.user,
            lecture=lecture,
            stage=error.stage,
            error_type=error.__class__.__name__,
            error_message=error.message,
        )
        LectureProcessingStateService.mark_failed(lecture=lecture, error_message=error.message)
        lecture.status = LectureStatus.FAILED
        lecture.slide_processing_status = SlideProcessingStatus.FAILED
        lecture.slide_processing_error = error.message
        lecture.slide_processing_stage = error.stage
        lecture.save(
            update_fields=[
                "status",
                "slide_processing_status",
                "slide_processing_error",
                "slide_processing_stage",
                "updated_at",
            ]
        )

    def complete_pipeline(self, *, lecture: Lecture, debug: dict[str, object]) -> None:
        lecture.processing_progress = 100
        lecture.slide_processing_status = SlideProcessingStatus.COMPLETED
        lecture.slide_processing_progress = 100
        lecture.slide_processing_stage = "COMPLETED"
        lecture.slide_processing_error = ""
        lecture.slide_processing_debug = debug
        lecture.slides_ready = True
        lecture.save(
            update_fields=[
                "processing_progress",
                "slide_processing_status",
                "slide_processing_progress",
                "slide_processing_stage",
                "slide_processing_error",
                "slide_processing_debug",
                "slides_ready",
                "updated_at",
            ]
        )

    def parse_document(
        self,
        *,
        slides_file: LectureFile,
        lightweight: bool = False,
    ) -> tuple[ParsedSlidesDocument, list[tuple[int, bytes]]]:
        with self.storage.open(slides_file.storage_path, "rb") as file_handle:
            file_bytes = file_handle.read()

        file_extension = slides_file.original_name.lower().split(".")[-1]
        if file_extension == "pdf":
            parsed = self.pdf_parser.parse(file_bytes=file_bytes, include_images=not lightweight)
            thumbnails = self.pdf_parser.render_thumbnails(file_bytes=file_bytes)
            return parsed, thumbnails

        if file_extension == "ppt":
            raise PipelineError(
                stage="PPTX_PARSE",
                message="Legacy PPT slides are not supported. Please convert the file to PPTX or PDF and upload it again.",
            )

        if file_extension == "pptx":
            parsed = self.pptx_parser.parse(file_bytes=file_bytes, include_images=not lightweight)
            return parsed, []

        raise PipelineError(
            stage="DETECT_FILE_TYPE", message=f"Unsupported slides file type: {file_extension}"
        )

    def enrich_pages(
        self, *, slides_file: LectureFile, parsed: ParsedSlidesDocument
    ) -> ParsedSlidesDocument:
        # Table extraction needs a seekable filesystem path. Object-backed files
        # are materialized only for this bounded extraction window.
        with self.storage.materialize_to_temp(slides_file.storage_path) as file_path:
            # Pages whose text was extracted directly never need OCR; only near-empty pages (text drawn as pictures) are read.
            minimum_text = int(settings.SLIDE_OCR_MIN_TEXT_CHARS)
            ocr_by_page = self.ocr_extractor.extract_pages(
                pages=[
                    (page.page_number, page.images)
                    for page in parsed.pages
                    if page.images and len((page.text or "").strip()) < minimum_text
                ]
            )
            for page in parsed.pages:
                ocr_output = ocr_by_page.get(page.page_number, [])
                page.ocr_text = "\n".join(
                    line["text"]
                    for block in ocr_output
                    for line in block.get("lines", [])
                    if line.get("text")
                )
                parsed.ocr_output.extend(ocr_output)

            for page in parsed.pages:
                if not page.tables:
                    page.tables = self.table_extractor.extract(
                        file_path=file_path, page_number=page.page_number
                    )
                parsed.extracted_tables.extend(page.tables)

                page.formulas = self.formula_extractor.extract(
                    page_number=page.page_number, images=page.images
                )
                parsed.extracted_formulas.extend(page.formulas)

        return parsed

    def assign_thumbnails(
        self, *, lecture: Lecture, parsed: ParsedSlidesDocument, thumbnails: list[tuple[int, bytes]]
    ) -> ParsedSlidesDocument:
        if thumbnails:
            thumbnail_map = self.thumbnail_generator.save_pdf_thumbnails(
                lecture=lecture,
                thumbnails=thumbnails,
            )
        else:
            thumbnail_map = self.thumbnail_generator.create_placeholder_thumbnails(
                lecture=lecture,
                page_numbers=[page.page_number for page in parsed.pages],
            )

        for page in parsed.pages:
            page.thumbnail_path = thumbnail_map.get(page.page_number, "")
        return parsed

    @transaction.atomic
    def store_slide_pages(self, *, lecture: Lecture, parsed: ParsedSlidesDocument) -> None:
        SlidePage.objects.filter(lecture=lecture).delete()
        SlidePage.objects.bulk_create(
            [
                SlidePage(
                    lecture=lecture,
                    page_number=page.page_number,
                    text=page.text,
                    structured_text=page.structured_text,
                    images=[
                        {key: value for key, value in image.items() if key != "bytes"}
                        for image in page.images
                    ],
                    tables=page.tables,
                    formulas=page.formulas,
                    ocr_text=page.ocr_text,
                    raw_extracted_text=page.raw_extracted_text,
                    thumbnail_path=page.thumbnail_path,
                )
                for page in parsed.pages
            ]
        )

    def build_debug_payload(
        self, *, lecture: Lecture, slides_file: LectureFile, parsed: ParsedSlidesDocument
    ) -> dict[str, object]:
        return {
            "lecture": {
                "id": str(lecture.id),
                "title": lecture.title,
                "status": lecture.status,
                "slide_processing_status": lecture.slide_processing_status,
            },
            "files": [
                {
                    "id": str(slides_file.id),
                    "name": slides_file.original_name,
                    "storage_path": slides_file.storage_path,
                    "mime_type": slides_file.mime_type,
                }
            ],
            "raw_pdf_text": parsed.raw_pdf_text,
            "ocr_output": parsed.ocr_output,
            "extracted_tables": parsed.extracted_tables,
            "extracted_formulas": parsed.extracted_formulas,
            "slide_pages_raw": parsed.slide_pages_raw,
        }

    def run_pipeline(self, *, lecture: Lecture, lightweight: bool = False) -> dict[str, object]:
        slides_file = self.get_slides_file(lecture=lecture)
        self.update_progress(lecture=lecture, stage="DETECT_FILE_TYPE", progress=10)
        with measure_scaling_stage(
            stage="slide_parse",
            lecture_id=str(lecture.id),
            input_size_bytes=slides_file.file_size,
            lightweight=lightweight,
        ) as measurement:
            parsed, thumbnails = self.parse_document(
                slides_file=slides_file, lightweight=lightweight
            )
            measurement.add(slide_count=len(parsed.pages))
        self.update_progress(lecture=lecture, stage="EXTRACT_PAGES", progress=25)
        with measure_scaling_stage(
            stage="slide_thumbnails",
            lecture_id=str(lecture.id),
            slide_count=len(parsed.pages),
        ):
            parsed = self.assign_thumbnails(lecture=lecture, parsed=parsed, thumbnails=thumbnails)
        self.update_progress(lecture=lecture, stage="GENERATE_THUMBNAILS", progress=50)
        if not lightweight:
            self.update_progress(lecture=lecture, stage="READ_SLIDE_IMAGES", progress=55)
            with measure_scaling_stage(
                stage="slide_ocr_enrichment",
                lecture_id=str(lecture.id),
                slide_count=len(parsed.pages),
            ):
                parsed = self.enrich_pages(slides_file=slides_file, parsed=parsed)
            self.update_progress(lecture=lecture, stage="EXTRACT_TABLES_AND_FORMULAS", progress=75)
        else:
            self.update_progress(
                lecture=lecture,
                stage="LIGHTWEIGHT_SLIDE_PARSE",
                progress=75,
                debug={"lightweight_mode": True},
            )
        with measure_scaling_stage(
            stage="slide_persistence",
            lecture_id=str(lecture.id),
            slide_count=len(parsed.pages),
        ):
            self.store_slide_pages(lecture=lecture, parsed=parsed)
        debug = self.build_debug_payload(lecture=lecture, slides_file=slides_file, parsed=parsed)
        if lightweight:
            debug["lightweight_mode"] = True
        self.complete_pipeline(lecture=lecture, debug=debug)
        return debug


def _note_artifact_ready(note: GeneratedNote) -> bool:
    """A completed note is readable once it has a PDF, or, when PDF rendering is deferred,
    once its HTML is saved (the PDF is then prepared by its own task)."""
    if note.pdf_path:
        return True
    return bool(getattr(settings, "NOTE_PDF_DEFERRED_ENABLED", False)) and bool(
        note.html_path or note.body_html
    )


class ProcessingDebugService:
    @staticmethod
    def _resolve_active_celery_status(*, lecture: Lecture, task_id: str, note_status: str) -> str:
        celery_status = "PENDING"
        if task_id:
            try:
                celery_status = AsyncResult(task_id).status
            except (OperationalError, RedisError, OSError):
                celery_status = "UNAVAILABLE"

        if lecture.status == LectureStatus.FAILED:
            return "FAILURE"
        if (
            lecture.status == LectureStatus.COMPLETED
            and note_status == NoteGenerationStatus.COMPLETED
        ):
            return "SUCCESS"
        if note_status in {
            NoteGenerationStatus.QUEUED,
            NoteGenerationStatus.SUMMARIZING,
            NoteGenerationStatus.STRUCTURING,
            NoteGenerationStatus.RENDERING,
        }:
            return "STARTED"
        if lecture.transcription_status in {
            TranscriptionStatus.QUEUED,
            TranscriptionStatus.TRANSCRIBING,
            TranscriptionStatus.CLEANING,
            TranscriptionStatus.MERGING,
        }:
            return "STARTED"
        if lecture.audio_processing_status in {
            AudioProcessingStatus.NORMALIZING,
            AudioProcessingStatus.CLEANING,
            AudioProcessingStatus.REMOVING_SILENCE,
            AudioProcessingStatus.SPEED_OPTIMIZING,
            AudioProcessingStatus.CHUNKING,
        }:
            return "STARTED"
        if lecture.slide_processing_status == SlideProcessingStatus.PROCESSING:
            return "STARTED"
        if lecture.status == LectureStatus.PROCESSING and celery_status == "SUCCESS":
            return "STARTED"
        return celery_status

    @staticmethod
    def raw_processing(*, lecture: Lecture) -> dict[str, object]:
        payload = lecture.slide_processing_debug or {}
        files = payload.get("files")
        if not files:
            files = [
                {
                    "id": str(file_obj.id),
                    "name": file_obj.original_name,
                    "storage_path": file_obj.storage_path,
                    "mime_type": file_obj.mime_type,
                    "upload_status": file_obj.upload_status,
                }
                for file_obj in lecture.files.all().order_by("-created_at")
            ]
        return {
            "lecture": payload.get("lecture", {"id": str(lecture.id), "title": lecture.title}),
            "files": files,
            "raw_pdf_text": payload.get("raw_pdf_text", []),
            "ocr_output": payload.get("ocr_output", []),
            "extracted_tables": payload.get("extracted_tables", []),
            "extracted_formulas": payload.get("extracted_formulas", []),
            "slide_pages_raw": payload.get("slide_pages_raw", []),
        }

    @staticmethod
    def pipeline_status(*, lecture: Lecture) -> dict[str, object]:
        enforce_lecture_processing_timeouts(lecture=lecture)
        lecture.refresh_from_db()
        processing_job = (
            lecture.processing_jobs.exclude(status__in=("SUCCEEDED", "FAILED"))
            .order_by("-attempt")
            .first()
        )
        scheduler_waiting = bool(
            processing_job is not None
            and processing_job.status == "QUEUED"
            and processing_job.scheduler_managed
            and processing_job.scheduler_dispatched_at is None
        )
        note_status, note_progress = aggregate_note_status(lecture=lecture)
        primary_note = next(
            (
                note
                for note in lecture.generated_notes.order_by("variant")
                if not is_comparison_note_variant(note.variant)
            ),
            None,
        )
        note_stage_detail = ""
        note_error_message = ""
        if primary_note is not None:
            note_stage_detail = str(
                (primary_note.generation_metadata or {}).get("current_stage_detail", "") or ""
            )
            note_error_message = str(primary_note.error_message or "")
        task_id = str(
            (lecture.processing_metadata or {}).get("pipeline_task_id")
            or lecture.slide_processing_task_id
            or ""
        )
        celery_status = ProcessingDebugService._resolve_active_celery_status(
            lecture=lecture,
            task_id=task_id,
            note_status=note_status,
        )
        started_at = str(lecture.processing_metadata.get("pipeline_started_at", "") or "")
        elapsed_seconds = 0
        estimated_remaining_seconds: int | None = None
        if started_at:
            try:
                started_dt = datetime.fromisoformat(started_at)
                elapsed_seconds = max(
                    0, int((datetime.now(dt_timezone.utc) - started_dt).total_seconds())
                )
            except ValueError:
                started_at = ""
        transcription_stage_progress = (
            100
            if lecture.transcription_status == TranscriptionStatus.COMPLETED
            else max(0, round((lecture.audio_processing_progress or 0) * 0.35))
            if lecture.audio_processing_status != AudioProcessingStatus.COMPLETED
            else max(35, round(35 + ((lecture.transcription_progress or 0) * 0.4)))
        )
        slides_stage_progress = (
            100
            if lecture.slide_processing_status
            in {SlideProcessingStatus.NOT_STARTED, SlideProcessingStatus.COMPLETED}
            else lecture.slide_processing_progress
        )
        note_stage_progress = (
            100
            if note_status == NoteGenerationStatus.COMPLETED
            else min(95, max(0, note_progress))
            if note_status != NoteGenerationStatus.NOT_STARTED
            else 0
        )
        pdf_stage_progress = 0
        if (
            note_status == NoteGenerationStatus.COMPLETED
            and primary_note is not None
            and _note_artifact_ready(primary_note)
        ):
            pdf_stage_progress = 100
        elif note_stage_detail == "GENERATING_PDF":
            pdf_stage_progress = 70
        overall_progress = round(
            (
                100
                + transcription_stage_progress
                + slides_stage_progress
                + note_stage_progress
                + pdf_stage_progress
            )
            / 5
        )
        if scheduler_waiting:
            overall_progress = 0
            estimated_remaining_seconds = None
        elif overall_progress > 0 and overall_progress < 100 and elapsed_seconds > 0:
            estimated_remaining_seconds = max(
                0, int((elapsed_seconds * (100 - overall_progress)) / overall_progress)
            )
        return {
            "lecture_id": lecture.id,
            "status": lecture.status,
            "processing_state": "SCHEDULER_WAITING"
            if scheduler_waiting
            else lecture.processing_state,
            "progress": overall_progress,
            "stage": (
                "SCHEDULER_WAIT"
                if scheduler_waiting
                else note_stage_detail
                or lecture.transcript_metadata.get("stage")
                or lecture.processing_metadata.get("pipeline_timeout_reason")
                or lecture.processing_metadata.get("note_generation_stage")
                or lecture.processing_metadata.get("audio_processing_stage")
                or lecture.slide_processing_stage
            ),
            "task_id": task_id,
            "celery_status": celery_status,
            "error_message": (
                note_error_message
                or lecture.transcript_metadata.get("error_message")
                or lecture.processing_metadata.get("pipeline_error")
                or lecture.processing_metadata.get("note_generation_stage_message")
                or lecture.processing_metadata.get("audio_processing_error")
                or lecture.slide_processing_error
            ),
            "slide_status": lecture.slide_processing_status,
            "slide_progress": lecture.slide_processing_progress,
            "audio_status": lecture.audio_processing_status,
            "audio_progress": lecture.audio_processing_progress,
            "transcription_status": lecture.transcription_status,
            "transcription_progress": lecture.transcription_progress,
            "note_status": note_status,
            "note_progress": note_progress,
            "input_language": lecture.input_language,
            "output_language": lecture.output_language,
            "note_detail_mode": lecture.note_detail_mode,
            "processing_started_at": lecture.processing_started_at.isoformat()
            if lecture.processing_started_at
            else "",
            "processing_completed_at": lecture.processing_completed_at.isoformat()
            if lecture.processing_completed_at
            else "",
            "started_at": started_at,
            "elapsed_seconds": elapsed_seconds,
            "estimated_remaining_seconds": estimated_remaining_seconds,
            "scheduler_state": "WAITING"
            if scheduler_waiting
            else "DISPATCHED"
            if processing_job and processing_job.scheduler_dispatched_at
            else "NONE",
            "scheduler_ready_at": processing_job.scheduler_ready_at.isoformat()
            if processing_job and processing_job.scheduler_ready_at
            else "",
        }

    @staticmethod
    def _aggregate_note_status_from_rows(
        notes: list[GeneratedNote],
    ) -> tuple[str, int]:
        # Mirrors apps.notes.generation.aggregate_note_status, but works from an
        # `.only(...)` queryset that never loads body_html/semantic_payload/
        # source_snapshot, so a status poll never pulls note content into memory.
        if not notes:
            return NoteGenerationStatus.NOT_STARTED, 0
        statuses = {note.status for note in notes}
        if NoteGenerationStatus.FAILED in statuses:
            return NoteGenerationStatus.FAILED, 100
        if all(status == NoteGenerationStatus.COMPLETED for status in statuses):
            return NoteGenerationStatus.COMPLETED, 100
        if NoteGenerationStatus.RENDERING in statuses:
            return NoteGenerationStatus.RENDERING, min(99, max(note.progress for note in notes))
        if NoteGenerationStatus.STRUCTURING in statuses:
            return NoteGenerationStatus.STRUCTURING, max(note.progress for note in notes)
        if NoteGenerationStatus.QUEUED in statuses:
            return NoteGenerationStatus.QUEUED, max(note.progress for note in notes)
        return NoteGenerationStatus.NOT_STARTED, max(note.progress for note in notes)

    @staticmethod
    def compact_status(*, lecture: Lecture) -> dict[str, object]:
        """Phase 10 compact status: durable DB state only, no content fields.

        This is a pure read. Unlike ``pipeline_status`` above, it does not call
        ``enforce_lecture_processing_timeouts`` (which can write a FAILED state on
        timeout) — the periodic ``cancel-timed-out-lectures`` Beat task already
        owns that reconciliation. Keeping this endpoint side-effect free is a
        deliberate Phase 10 requirement, not an oversight.
        """
        processing_job = (
            lecture.processing_jobs.exclude(
                status__in=(LectureProcessingJobStatus.SUCCEEDED, LectureProcessingJobStatus.FAILED)
            )
            .order_by("-attempt")
            .first()
        )
        job_pending = bool(
            processing_job is not None and processing_job.status == LectureProcessingJobStatus.PENDING
        )
        scheduler_waiting = bool(
            processing_job is not None
            and processing_job.status == LectureProcessingJobStatus.QUEUED
            and processing_job.scheduler_managed
            and processing_job.scheduler_dispatched_at is None
        )

        primary_variant = normalize_note_variant(settings.NOTE_PRIMARY_MODEL)
        note_rows = list(
            lecture.generated_notes.only(
                "id",
                "lecture_id",
                "variant",
                "status",
                "progress",
                "pdf_path",
                "generation_metadata",
                "error_message",
            ).order_by("variant")
        )
        # The admin-only comparison note runs after the lecture finished; it never drives status.
        note_rows = [note for note in note_rows if not is_comparison_note_variant(note.variant)]
        note_status, note_progress = ProcessingDebugService._aggregate_note_status_from_rows(note_rows)
        primary_note = next(
            (note for note in note_rows if note.variant == primary_variant),
            note_rows[0] if note_rows else None,
        )
        note_stage_detail = ""
        note_error_message = ""
        note_id = ""
        note_ready = False
        if primary_note is not None:
            note_stage_detail = str(
                (primary_note.generation_metadata or {}).get("current_stage_detail", "") or ""
            )
            note_error_message = str(primary_note.error_message or "")
            note_id = str(primary_note.id)
            note_ready = note_status == NoteGenerationStatus.COMPLETED and _note_artifact_ready(primary_note)

        if job_pending:
            processing_state = "PENDING"
        elif scheduler_waiting:
            processing_state = "SCHEDULER_WAITING"
        elif lecture.status == LectureStatus.COMPLETED:
            processing_state = "COMPLETED"
        elif lecture.status == LectureStatus.FAILED:
            processing_state = "FAILED"
        else:
            processing_state = lecture.processing_state

        elapsed_seconds = 0
        if lecture.processing_started_at:
            elapsed_seconds = max(
                0, int((timezone.now() - lecture.processing_started_at).total_seconds())
            )

        # The note is held back until the slides are read (see dispatch_note_generation).
        waiting_for_slides = bool(
            not note_stage_detail
            and note_status == NoteGenerationStatus.NOT_STARTED
            and lecture.transcription_status == TranscriptionStatus.COMPLETED
            and lecture.slide_processing_status
            in {SlideProcessingStatus.QUEUED, SlideProcessingStatus.PROCESSING}
        )
        stage = (
            "SCHEDULER_WAIT"
            if scheduler_waiting
            else WAITING_FOR_SLIDES
            if waiting_for_slides
            else note_stage_detail
            or lecture.transcript_metadata.get("stage_detail")
            or lecture.processing_metadata.get("pipeline_timeout_reason")
            or lecture.processing_metadata.get("note_generation_stage")
            or lecture.processing_metadata.get("audio_processing_stage")
            or lecture.slide_processing_stage
            or ""
        )
        error_message = (
            note_error_message
            or lecture.transcript_metadata.get("error_message")
            or lecture.processing_metadata.get("pipeline_error")
            or lecture.processing_metadata.get("note_generation_stage_message")
            or lecture.processing_metadata.get("audio_processing_error")
            or lecture.slide_processing_error
            or ""
        )

        return {
            "lecture_id": lecture.id,
            "status": lecture.status,
            "processing_state": processing_state,
            "stage": stage,
            "error_message": error_message,
            "elapsed_seconds": elapsed_seconds,
            "scheduler_state": "WAITING"
            if scheduler_waiting
            else "DISPATCHED"
            if processing_job and processing_job.scheduler_dispatched_at
            else "NONE",
            "slide_status": lecture.slide_processing_status,
            "slide_progress": lecture.slide_processing_progress,
            "slide_stage": lecture.slide_processing_stage or "",
            "slide_count": lecture.slide_pages.count(),
            "audio_status": lecture.audio_processing_status,
            "audio_progress": lecture.audio_processing_progress,
            "transcription_status": lecture.transcription_status,
            "transcription_progress": lecture.transcription_progress,
            "transcription_chunk_count": lecture.audio_chunks.count(),
            "transcription_processed_chunk_count": lecture.transcript_chunks.count(),
            "transcription_stage_detail": str(lecture.transcript_metadata.get("stage_detail", "") or ""),
            "note_status": note_status,
            "note_progress": note_progress,
            "note_id": note_id,
            "note_ready": note_ready,
            "updated_at": lecture.updated_at.isoformat() if lecture.updated_at else "",
        }


def _run_task_in_local_background(*, lecture_id: str, job_id: str = "") -> None:
    close_old_connections()
    try:
        orchestrator = LectureProcessingOrchestrator()
        lecture = Lecture.objects.get(id=lecture_id)
        if job_id:
            started_job = ProcessingJobService.mark_started(job_id=job_id)
            if (
                started_job is not None
                and started_job.scheduler_managed
                and getattr(started_job, "_scheduler_started_now", False)
            ):
                orchestrator.start(lecture=lecture, task_id=started_job.celery_task_id)
                lecture.refresh_from_db()
        timeout_error = enforce_lecture_processing_timeouts(lecture=lecture)
        if timeout_error is not None:
            logger.warning(
                "lecture_processing_timed_out",
                extra={"lecture_id": lecture_id, "stage": timeout_error.stage},
            )
            return
        try:
            orchestrator.run(lecture=lecture)
        except PipelineError as exc:
            orchestrator.fail(lecture=lecture, error=exc)
            if exc.stage in {"PROCESSING_TIMEOUT", "TRANSCRIPTION_TIMEOUT"}:
                logger.warning(
                    "lecture_processing_timed_out",
                    extra={"lecture_id": lecture_id, "stage": exc.stage},
                )
            else:
                logger.exception(
                    "lecture_processing_failed",
                    extra={"lecture_id": lecture_id, "stage": exc.stage},
                )
        except Exception as exc:  # pragma: no cover - defensive local-thread fallback
            error = PipelineError(stage="SYSTEM", message=str(exc))
            orchestrator.fail(lecture=lecture, error=error)
            logger.exception(
                "lecture_processing_failed", extra={"lecture_id": lecture_id, "stage": error.stage}
            )
    finally:
        close_old_connections()


def launch_lecture_processing(
    *,
    lecture_id: str,
    task_id: str,
    job_id: str = "",
    worker_host_id: str | None = None,
) -> None:
    """Dispatch the pipeline task, optionally pinned to a specific worker
    host's exclusive queue (Phase 16: `pipeline-<host_id>`).

    `worker_host_id=None` (the default, and the only value ever passed when
    `DISTRIBUTED_CAPACITY_ENABLED` is False) omits `queue=` entirely, so
    Celery falls back to the static `CELERY_TASK_ROUTES` mapping exactly as
    before Phase 16 — this call is a strict no-op change for single-host
    deployments.
    """
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        worker = Thread(
            target=_run_task_in_local_background,
            kwargs={"lecture_id": lecture_id, "job_id": job_id},
            name=f"tesanote-lecture-pipeline-{lecture_id}",
            daemon=True,
        )
        worker.start()
        return

    from apps.ai_pipeline.stage_metrics import StageConcurrencyStore
    from apps.ai_pipeline.tasks import process_lecture_pipeline

    apply_kwargs: dict[str, object] = {"args": [lecture_id, job_id], "task_id": task_id}
    queue_name = f"pipeline-{worker_host_id}" if worker_host_id else "pipeline"
    if worker_host_id:
        apply_kwargs["queue"] = queue_name

    try:
        StageConcurrencyStore.record_enqueued(queue=queue_name, task_id=task_id)
        process_lecture_pipeline.apply_async(**apply_kwargs)
    except (OperationalError, RedisError, OSError, TimeoutError) as exc:
        raise PipelineError(
            stage="PIPELINE_DISPATCH",
            message="The processing queue is temporarily unavailable. Please try again in a moment.",
        ) from exc
    except Exception as exc:  # pragma: no cover - defensive queue dispatch guard
        raise PipelineError(
            stage="PIPELINE_DISPATCH",
            message="The processing job could not be started. Please try again.",
        ) from exc
