from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.ai_pipeline.exceptions import PipelineError
from apps.audio_pipeline.chunking.service import AudioChunkingService
from apps.audio_pipeline.metadata.service import AudioMetadataService
from apps.audio_pipeline.models import AudioChunk
from apps.audio_pipeline.processors.ffmpeg import FFmpegProcessor
from apps.audio_pipeline.types import (
    AudioChunkSpec,
    AudioProcessingResult,
    Segment,
    SegmentOptimizationResult,
    TimestampMapping,
)
from apps.audio_pipeline.vad.silero import SileroVADService
from apps.audio_pipeline.waveform.service import WaveformPreviewService
from apps.lectures.models import Lecture, LectureStatus, TranscriptionStatus
from apps.storage_lifecycle.paths import temporary_artifact_path
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from apps.uploads.models import LectureFile, LectureFileType, UploadStatus
from infrastructure.observability.scaling import measure_scaling_stage
from infrastructure.storage.factory import get_storage_service
from services.lectures.processing import LectureProcessingStateService

logger = logging.getLogger("tesanote")


class AudioProcessingDebugService:
    @staticmethod
    def debug_payload(*, lecture: Lecture) -> dict[str, object]:
        metadata = lecture.processing_metadata or {}
        live_chunk_count = lecture.audio_chunks.count()
        return {
            "original_duration": metadata.get("original_duration", 0.0),
            "optimized_duration": metadata.get("optimized_duration", lecture.optimized_audio_duration or 0.0),
            "silence_removed_seconds": metadata.get("silence_removed_seconds", lecture.silence_removed_seconds or 0.0),
            "speed_multiplier": metadata.get("speed_multiplier", 1.5),
            "chunk_count": live_chunk_count,
            "estimated_cost_reduction_percent": metadata.get("estimated_cost_reduction_percent", 0.0),
            "estimated_billable_units": metadata.get("estimated_billable_units", 0),
            "ideal_billable_units": metadata.get("ideal_billable_units", 0),
            "estimated_billing_unit_overhead": metadata.get("estimated_billing_unit_overhead", 0),
            "speech_ratio": metadata.get("speech_ratio", 0.0),
            "chunks": metadata.get(
                "chunks",
                [
                    {
                        "chunk_index": chunk.chunk_index,
                        "start_ts": chunk.start_ts,
                        "end_ts": chunk.end_ts,
                        "duration_seconds": chunk.duration_seconds,
                        "optimized_audio_path": chunk.optimized_audio_path,
                    }
                    for chunk in lecture.audio_chunks.all().order_by("chunk_index")
                ],
            ),
            "processing_steps": metadata.get("processing_steps", []),
            "waveform_preview_data": metadata.get("waveform_preview_data", []),
            "status": lecture.audio_processing_status,
            "stage": metadata.get("audio_processing_stage", ""),
            "progress": lecture.audio_processing_progress,
            "error_message": metadata.get("audio_processing_error", ""),
        }


class AudioPreprocessingService:
    default_speed_multiplier = 1.5

    def __init__(self) -> None:
        self.storage = get_storage_service()
        self.ffmpeg = FFmpegProcessor(
            ffmpeg_binary=settings.FFMPEG_BINARY,
            ffprobe_binary=settings.FFPROBE_BINARY,
        )
        self.vad = SileroVADService()
        self.chunking = AudioChunkingService()
        self.waveform = WaveformPreviewService()
        self.metadata = AudioMetadataService()
        self.transcription_max_chunk_seconds = float(settings.AVANEGAR_MAX_CHUNK_DURATION_SECONDS)
        self.transcription_target_chunk_seconds = float(settings.AVANEGAR_TARGET_CHUNK_DURATION_SECONDS)
        self.transcription_min_chunk_seconds = float(settings.GAPGPT_TRANSCRIPTION_MIN_CHUNK_SECONDS)
        self.transcription_billing_unit_seconds = float(settings.AVANEGAR_BILLING_UNIT_SECONDS)
        self.min_speech_ratio_floor = float(settings.AUDIO_MIN_SPEECH_RATIO_FLOOR)
        self.min_removable_silence_seconds = float(settings.AUDIO_MIN_REMOVABLE_SILENCE_SECONDS)
        self._working_directory: Path | None = None

    @staticmethod
    def _build_segments_summary(*, speech_segments: list[Segment], total_duration: float) -> tuple[list[Segment], list[Segment], float]:
        merged_segments: list[Segment] = []
        for segment in speech_segments:
            normalized = Segment(
                start=round(max(0.0, segment.start), 3),
                end=round(min(total_duration, segment.end), 3),
            )
            if normalized.duration <= 0:
                continue
            if merged_segments and normalized.start <= merged_segments[-1].end:
                merged_segments[-1].end = round(max(merged_segments[-1].end, normalized.end), 3)
            else:
                merged_segments.append(normalized)

        silence_segments: list[Segment] = []
        current = 0.0
        for segment in merged_segments:
            if segment.start > current:
                silence_segments.append(Segment(start=round(current, 3), end=round(segment.start, 3)))
            current = segment.end
        if current < total_duration:
            silence_segments.append(Segment(start=round(current, 3), end=round(total_duration, 3)))

        speech_duration = sum(segment.duration for segment in merged_segments)
        speech_ratio = round(speech_duration / total_duration, 4) if total_duration else 0.0
        return merged_segments, silence_segments, speech_ratio

    def _coarse_preserve_segments(self, *, speech_segments: list[Segment], total_duration: float) -> tuple[list[Segment], list[Segment], float]:
        if not speech_segments:
            return self._build_segments_summary(
                speech_segments=[Segment(start=0.0, end=round(total_duration, 3))],
                total_duration=total_duration,
            )

        padding = max(float(settings.AUDIO_VAD_PADDING_SECONDS) * 2.5, 1.2)
        bridge_gap = 1.8
        if total_duration > 180:
            padding = max(padding, 2.0)
            bridge_gap = 2.8
        expanded_segments = [
            Segment(
                start=round(max(0.0, segment.start - padding), 3),
                end=round(min(total_duration, segment.end + padding), 3),
            )
            for segment in speech_segments
        ]
        bridged_segments: list[Segment] = []
        for segment in expanded_segments:
            if bridged_segments and segment.start - bridged_segments[-1].end <= bridge_gap:
                bridged_segments[-1].end = round(max(bridged_segments[-1].end, segment.end), 3)
            else:
                bridged_segments.append(segment)

        return self._build_segments_summary(speech_segments=bridged_segments, total_duration=total_duration)

    def _preserve_short_pauses(self, *, speech_segments: list[Segment], total_duration: float) -> tuple[list[Segment], list[Segment], float]:
        if not speech_segments:
            return [], [Segment(start=0.0, end=round(total_duration, 3))], 0.0

        gap_threshold = max(self.min_removable_silence_seconds, 1.2)
        if total_duration > 180:
            gap_threshold = max(gap_threshold, 1.8)

        protected_segments: list[Segment] = []
        for segment in speech_segments:
            if not protected_segments:
                protected_segments.append(Segment(start=segment.start, end=segment.end))
                continue

            previous = protected_segments[-1]
            gap = segment.start - previous.end
            previous_short = previous.duration <= 2.4
            current_short = segment.duration <= 2.4
            adaptive_threshold = gap_threshold + (0.5 if previous_short or current_short else 0.0)

            if gap <= adaptive_threshold:
                previous.end = round(max(previous.end, segment.end), 3)
            else:
                protected_segments.append(Segment(start=segment.start, end=segment.end))

        return self._build_segments_summary(speech_segments=protected_segments, total_duration=total_duration)

    def get_audio_file(self, *, lecture: Lecture) -> LectureFile:
        audio_file = (
            LectureFile.objects.filter(
                lecture=lecture,
                file_type=LectureFileType.AUDIO,
                upload_status=UploadStatus.UPLOADED,
            )
            .order_by("-created_at")
            .first()
        )
        if audio_file is None:
            raise PipelineError(stage="VALIDATION", message="No uploaded audio file found for this lecture.")
        if int(audio_file.duration_seconds or 0) > settings.AUDIO_MAX_DURATION_SECONDS:
            raise PipelineError(stage="VALIDATION", message="مدت زمان فایل صوتی نباید بیشتر از ۹۰ دقیقه باشد.")
        if not self.storage.exists(audio_file.storage_path):
            raise PipelineError(
                stage="AUDIO_INPUT",
                message="فایل صوتی بارگذاری‌شده در دسترس نیست. لطفاً فایل را دوباره بارگذاری کنید.",
            )
        return audio_file

    def start_processing(self, *, lecture: Lecture) -> None:
        lecture.status = LectureStatus.PROCESSING
        lecture.audio_processing_status = "QUEUED"
        lecture.audio_processing_progress = 0
        lecture.optimized_audio_duration = None
        lecture.silence_removed_seconds = 0
        lecture.processing_metadata = {
            **lecture.processing_metadata,
            "audio_processing_stage": "QUEUED",
            "audio_processing_error": "",
            "audio_chunk_generation_complete": False,
        }
        lecture.save(
            update_fields=[
                "status",
                "audio_processing_status",
                "audio_processing_progress",
                "optimized_audio_duration",
                "silence_removed_seconds",
                "processing_metadata",
                "updated_at",
            ]
        )

    def update_progress(self, *, lecture: Lecture, stage: str, progress: int, extra: dict[str, object] | None = None) -> None:
        lecture.audio_processing_status = stage
        lecture.audio_processing_progress = progress
        lecture.processing_progress = max(lecture.slide_processing_progress, progress)
        metadata = {**lecture.processing_metadata, "audio_processing_stage": stage}
        if extra:
            metadata.update(extra)
        lecture.processing_metadata = metadata
        lecture.save(
            update_fields=[
                "audio_processing_status",
                "audio_processing_progress",
                "processing_progress",
                "processing_metadata",
                "updated_at",
            ]
        )

    def fail_pipeline(self, *, lecture: Lecture, error: PipelineError) -> None:
        LectureProcessingStateService.mark_failed(lecture=lecture, error_message=error.message)
        lecture.audio_processing_status = "FAILED"
        lecture.processing_progress = max(lecture.slide_processing_progress, lecture.audio_processing_progress)
        lecture.processing_metadata = {
            **lecture.processing_metadata,
            "audio_processing_error": error.message,
            "audio_processing_stage": error.stage,
        }
        lecture.save(update_fields=["audio_processing_status", "processing_progress", "processing_metadata", "updated_at"])

    def complete_pipeline(self, *, lecture: Lecture, result: AudioProcessingResult) -> None:
        lecture.audio_processing_status = "COMPLETED"
        lecture.audio_processing_progress = 100
        lecture.processing_progress = max(lecture.slide_processing_progress, 100)
        lecture.optimized_audio_duration = round(result.optimized_duration, 3)
        lecture.silence_removed_seconds = round(result.silence_removed_seconds, 3)
        lecture.processing_metadata = {
            **lecture.processing_metadata,
            "audio_processing_stage": "COMPLETED",
            "audio_processing_error": "",
            "original_duration": round(result.original_duration, 3),
            "optimized_duration": round(result.optimized_duration, 3),
            "silence_removed_seconds": round(result.silence_removed_seconds, 3),
            "speed_multiplier": result.speed_multiplier,
            "chunk_count": result.chunk_count,
            "estimated_cost_reduction_percent": result.estimated_cost_reduction_percent,
            "speech_ratio": result.speech_ratio,
            "estimated_billable_units": next(
                (
                    step.get("estimated_billable_units")
                    for step in result.processing_steps
                    if step.get("stage") == "TRANSCRIPTION_CHUNK_POLICY"
                ),
                0,
            ),
            "ideal_billable_units": next(
                (
                    step.get("ideal_billable_units")
                    for step in result.processing_steps
                    if step.get("stage") == "TRANSCRIPTION_CHUNK_POLICY"
                ),
                0,
            ),
            "estimated_billing_unit_overhead": next(
                (
                    step.get("estimated_billing_unit_overhead")
                    for step in result.processing_steps
                    if step.get("stage") == "TRANSCRIPTION_CHUNK_POLICY"
                ),
                0,
            ),
            "chunks": [
                {
                    "chunk_index": spec.chunk_index,
                    "optimized_start": spec.optimized_start,
                    "optimized_end": spec.optimized_end,
                    "original_start": spec.original_start,
                    "original_end": spec.original_end,
                    "duration_seconds": round(spec.duration_seconds, 3),
                    "estimated_billable_units": self.metadata.billable_units_for_duration(
                        duration_seconds=spec.duration_seconds,
                        billing_unit_seconds=self.transcription_billing_unit_seconds,
                    ),
                }
                for spec in result.chunk_specs
            ],
            "processing_steps": result.processing_steps,
            "waveform_preview_data": result.waveform_preview_data,
            "speech_segments": result.speech_segments,
            "silence_segments": result.silence_segments,
            "timestamp_mappings": result.timestamp_mappings,
            "normalized_audio_path": result.normalized_audio_path,
            "optimized_audio_path": result.optimized_audio_path,
            "audio_processing_mode": result.processing_mode,
            "audio_chunk_generation_complete": True,
        }
        lecture.save(
            update_fields=[
                "audio_processing_status",
                "audio_processing_progress",
                "processing_progress",
                "optimized_audio_duration",
                "silence_removed_seconds",
                "processing_metadata",
                "updated_at",
            ]
        )

    def _build_output_path(self, *, lecture_id: str, category: str, filename: str) -> str:
        if self._working_directory is None:
            raise PipelineError(stage="STORAGE", message="Audio processing workspace is unavailable.")
        return str(self._working_directory / lecture_id / "audio" / category / filename)

    def _temporary_storage_path(self, *, lecture: Lecture, category: str, filename: str) -> str:
        return temporary_artifact_path(
            user_id=str(lecture.user_id),
            lecture_id=str(lecture.id),
            category=category,
            filename=filename,
        )

    def _persist_output(
        self,
        *,
        lecture: Lecture,
        local_path: str,
        relative_path: str,
        content_type: str,
        artifact_type: str,
    ) -> str:
        stored = self.storage.save_file(
            relative_path=relative_path,
            local_path=local_path,
            content_type=content_type,
        )
        StorageArtifactLifecycleService.register_temporary(
            user_id=str(lecture.user_id),
            lecture_id=str(lecture.id),
            storage_path=stored.path,
            size_bytes=stored.size,
            artifact_type=artifact_type,
        )
        return stored.path

    def _effective_speed_multiplier(self, *, speech_duration: float) -> float:
        if speech_duration <= 0:
            return 1.0
        return self.default_speed_multiplier

    def _normalization_filter(self, *, fast_mode: bool) -> str:
        filters = ["highpass=f=80", "lowpass=f=7600"]
        if not fast_mode:
            if settings.AUDIO_ENABLE_LOUDNORM:
                filters.insert(0, "loudnorm=I=-16:LRA=11:TP=-1.5")
            if settings.AUDIO_ENABLE_HEAVY_DENOISE:
                filters.append("afftdn=nf=-25")
        return ",".join(filters)

    def _segment_batch_size(self) -> int:
        return max(1, int(settings.TRANSCRIPTION_BATCH_SIZE))

    def _should_use_segmented_pipeline(self, *, duration_seconds: float) -> bool:
        return bool(settings.AUDIO_SEGMENTED_PIPELINE_ENABLED) and duration_seconds >= max(
            1, settings.AUDIO_SEGMENTED_PIPELINE_MIN_DURATION_SECONDS
        )

    def _set_chunk_generation_complete(self, *, lecture: Lecture, complete: bool) -> None:
        lecture.processing_metadata = {
            **lecture.processing_metadata,
            "audio_chunk_generation_complete": complete,
        }
        lecture.save(update_fields=["processing_metadata", "updated_at"])

    @staticmethod
    def _cleanup_temp_paths(*, paths: list[str]) -> None:
        for path in paths:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                continue

    def _speech_mappings(self, *, speech_segments: list[Segment], speed_multiplier: float) -> list[TimestampMapping]:
        mappings: list[TimestampMapping] = []
        optimized_cursor = 0.0
        for segment in speech_segments:
            duration = segment.duration
            optimized_duration = duration / speed_multiplier
            mappings.append(
                TimestampMapping(
                    original_start=round(segment.start, 3),
                    original_end=round(segment.end, 3),
                    optimized_start=round(optimized_cursor, 3),
                    optimized_end=round(optimized_cursor + optimized_duration, 3),
                )
            )
            optimized_cursor += optimized_duration
        return mappings

    def _build_segment_chunk_specs(self, *, duration_seconds: float) -> list[AudioChunkSpec]:
        chunk_specs: list[AudioChunkSpec] = []
        segment_seconds = max(1.0, min(float(settings.AUDIO_SEGMENT_SECONDS), self.transcription_max_chunk_seconds - 0.1))
        overlap_seconds = max(0.0, float(settings.AUDIO_SEGMENT_OVERLAP_SECONDS))
        if overlap_seconds >= segment_seconds:
            overlap_seconds = 0.0
        step_seconds = max(1.0, segment_seconds - overlap_seconds)
        cursor = 0.0
        chunk_index = 0
        while cursor < duration_seconds:
            end = min(duration_seconds, cursor + segment_seconds)
            chunk_specs.append(
                AudioChunkSpec(
                    chunk_index=chunk_index,
                    optimized_start=round(cursor, 3),
                    optimized_end=round(end, 3),
                    original_start=round(cursor, 3),
                    original_end=round(end, 3),
                )
            )
            chunk_index += 1
            cursor = round(cursor + step_seconds, 3)
        return chunk_specs

    def _optimize_segment_chunk(
        self,
        *,
        lecture: Lecture,
        original_path: str,
        spec: AudioChunkSpec,
        normalization_filter: str,
    ) -> SegmentOptimizationResult:
        temp_normalized_path = self._build_output_path(
            lecture_id=str(lecture.id),
            category="tmp",
            filename=f"segment-{spec.chunk_index:03d}-normalized.wav",
        )
        temp_optimized_path = self._build_output_path(
            lecture_id=str(lecture.id),
            category="tmp",
            filename=f"segment-{spec.chunk_index:03d}-optimized.wav",
        )
        segment_filename = f"segment-{spec.chunk_index:03d}.mp3"
        chunk_local_path = self._build_output_path(lecture_id=str(lecture.id), category="tmp", filename=segment_filename)
        chunk_relative_path = str(Path(settings.LECTURE_UPLOAD_ROOT) / str(lecture.id) / "audio" / "tmp" / segment_filename)
        temp_paths = [temp_normalized_path, temp_optimized_path, chunk_local_path]

        self.ffmpeg.ensure_parent(file_path=temp_normalized_path)
        self.ffmpeg.ensure_parent(file_path=temp_optimized_path)
        self.ffmpeg.ensure_parent(file_path=chunk_local_path)

        segment_original_duration = round(spec.original_end - spec.original_start, 3)
        self.ffmpeg.transcode_segment_to_wav(
            input_path=original_path,
            output_path=temp_normalized_path,
            start_ts=spec.original_start,
            duration_seconds=segment_original_duration,
            audio_filter=normalization_filter,
            threads=settings.AUDIO_FFMPEG_THREADS,
        )

        speech_segments, silence_segments, speech_ratio = self.vad.detect(wav_path=temp_normalized_path)
        detected_speech_ratio = speech_ratio
        speech_duration = sum(segment.duration for segment in speech_segments)
        used_conservative_audio_fallback = False
        if segment_original_duration <= 600 and (
            speech_ratio < self.min_speech_ratio_floor or speech_duration < max(20.0, segment_original_duration * 0.5)
        ):
            speech_segments, silence_segments, speech_ratio = self._coarse_preserve_segments(
                speech_segments=speech_segments,
                total_duration=segment_original_duration,
            )
            speech_duration = sum(segment.duration for segment in speech_segments)
            used_conservative_audio_fallback = True

        speech_segments, silence_segments, speech_ratio = self._preserve_short_pauses(
            speech_segments=speech_segments,
            total_duration=segment_original_duration,
        )
        speech_duration = sum(segment.duration for segment in speech_segments)
        effective_speed_multiplier = self._effective_speed_multiplier(speech_duration=speech_duration)

        self.ffmpeg.build_speech_only_audio(
            input_path=temp_normalized_path,
            output_path=temp_optimized_path,
            speech_segments=speech_segments,
            speed_multiplier=effective_speed_multiplier,
            threads=settings.AUDIO_FFMPEG_THREADS,
        )

        local_mappings = self._speech_mappings(speech_segments=speech_segments, speed_multiplier=effective_speed_multiplier)
        expected_optimized_duration = round(speech_duration / effective_speed_multiplier, 3) if speech_duration > 0 else 0.0
        optimized_duration = self.ffmpeg.probe_duration(input_path=temp_optimized_path)
        used_safe_speedup_fallback = False
        duration_mismatch = abs(optimized_duration - expected_optimized_duration)
        if expected_optimized_duration > 0 and duration_mismatch > max(2.0, expected_optimized_duration * 0.2):
            self.ffmpeg.build_speech_only_audio(
                input_path=temp_normalized_path,
                output_path=temp_optimized_path,
                speech_segments=speech_segments,
                speed_multiplier=1.0,
                threads=settings.AUDIO_FFMPEG_THREADS,
            )
            optimized_duration = self.ffmpeg.probe_duration(input_path=temp_optimized_path)
            used_safe_speedup_fallback = True
            effective_speed_multiplier = 1.0
            local_mappings = self._speech_mappings(speech_segments=speech_segments, speed_multiplier=effective_speed_multiplier)

        chunk_end_ts = max(0.1, optimized_duration)
        self.ffmpeg.extract_chunk(
            input_path=temp_optimized_path,
            output_path=chunk_local_path,
            start_ts=0.0,
            end_ts=chunk_end_ts,
        )

        return SegmentOptimizationResult(
            chunk_relative_path=chunk_relative_path,
            chunk_local_path=chunk_local_path,
            optimized_duration=round(optimized_duration, 3),
            speech_duration=round(speech_duration, 3),
            silence_removed_seconds=round(max(0.0, segment_original_duration - speech_duration), 3),
            speed_multiplier=effective_speed_multiplier,
            speech_ratio=speech_ratio,
            detected_speech_ratio=detected_speech_ratio,
            used_safe_speedup_fallback=used_safe_speedup_fallback,
            used_conservative_audio_fallback=used_conservative_audio_fallback,
            speech_segments=speech_segments,
            silence_segments=silence_segments,
            local_mappings=local_mappings,
            temp_paths=temp_paths,
        )

    def _target_chunk_seconds(self) -> float:
        return max(
            self.transcription_min_chunk_seconds,
            min(self.transcription_target_chunk_seconds, self.transcription_max_chunk_seconds - 0.1),
        )

    def _finalize_segment_group(
        self,
        *,
        lecture: Lecture,
        source_audio_path: str,
        group: list[tuple[AudioChunkSpec, SegmentOptimizationResult]],
        chunk_index: int,
        optimized_start: float,
    ) -> tuple[AudioChunkSpec, dict[str, object], list[dict[str, float]], list[dict[str, float]], list[dict[str, float]]]:
        combined_duration = round(sum(result.optimized_duration for _, result in group), 3)
        original_start = group[0][0].original_start
        original_end = group[-1][0].original_end
        final_spec = AudioChunkSpec(
            chunk_index=chunk_index,
            optimized_start=round(optimized_start, 3),
            optimized_end=round(optimized_start + combined_duration, 3),
            original_start=round(original_start, 3),
            original_end=round(original_end, 3),
        )
        chunk_filename = f"chunk-{chunk_index:03d}.mp3"
        chunk_local_path = self._build_output_path(lecture_id=str(lecture.id), category="chunks", filename=chunk_filename)
        chunk_relative_path = self._temporary_storage_path(
            lecture=lecture,
            category="audio/chunks",
            filename=chunk_filename,
        )
        self.ffmpeg.ensure_parent(file_path=chunk_local_path)
        self.ffmpeg.concat_audio_files(
            input_paths=[result.chunk_local_path for _, result in group],
            output_path=chunk_local_path,
        )
        self._persist_output(
            lecture=lecture,
            local_path=chunk_local_path,
            relative_path=chunk_relative_path,
            content_type="audio/mpeg",
            artifact_type="audio_chunk",
        )
        self._upsert_chunk(
            lecture=lecture,
            source_audio_path=source_audio_path,
            spec=final_spec,
            optimized_audio_path=chunk_relative_path,
        )
        chunk_record = {
            "chunk_index": final_spec.chunk_index,
            "optimized_audio_path": chunk_relative_path,
            "optimized_start": final_spec.optimized_start,
            "optimized_end": final_spec.optimized_end,
            "original_start": final_spec.original_start,
            "original_end": final_spec.original_end,
            "duration_seconds": round(final_spec.duration_seconds, 3),
            "estimated_billable_units": self.metadata.billable_units_for_duration(
                duration_seconds=final_spec.duration_seconds,
                billing_unit_seconds=self.transcription_billing_unit_seconds,
            ),
        }
        speech_segments: list[dict[str, float]] = []
        silence_segments: list[dict[str, float]] = []
        timestamp_mappings: list[dict[str, float]] = []
        optimized_cursor = final_spec.optimized_start
        for spec, result in group:
            for segment in result.speech_segments:
                speech_segments.append(
                    {
                        "start": round(spec.original_start + segment.start, 3),
                        "end": round(spec.original_start + segment.end, 3),
                    }
                )
            for segment in result.silence_segments:
                silence_segments.append(
                    {
                        "start": round(spec.original_start + segment.start, 3),
                        "end": round(spec.original_start + segment.end, 3),
                    }
                )
            for mapping in result.local_mappings:
                timestamp_mappings.append(
                    {
                        "original_start": round(spec.original_start + mapping.original_start, 3),
                        "original_end": round(spec.original_start + mapping.original_end, 3),
                        "optimized_start": round(optimized_cursor + mapping.optimized_start, 3),
                        "optimized_end": round(optimized_cursor + mapping.optimized_end, 3),
                    }
                )
            optimized_cursor = round(optimized_cursor + result.optimized_duration, 3)
        return final_spec, chunk_record, speech_segments, silence_segments, timestamp_mappings

    @transaction.atomic
    def store_chunks(self, *, lecture: Lecture, source_audio_path: str, chunk_specs: list[AudioChunkSpec], chunk_records: list[dict[str, object]]) -> None:
        AudioChunk.objects.filter(lecture=lecture).delete()
        chunk_path_by_index = {
            int(record["chunk_index"]): str(record["optimized_audio_path"])
            for record in chunk_records
        }
        AudioChunk.objects.bulk_create(
            [
                AudioChunk(
                    lecture=lecture,
                    chunk_index=spec.chunk_index,
                    start_ts=spec.original_start,
                    end_ts=spec.original_end,
                    duration_seconds=round(spec.duration_seconds, 3),
                    original_audio_path=source_audio_path,
                    optimized_audio_path=chunk_path_by_index.get(spec.chunk_index, ""),
                )
                for spec in chunk_specs
            ]
        )

    def _upsert_chunk(
        self,
        *,
        lecture: Lecture,
        source_audio_path: str,
        spec: AudioChunkSpec,
        optimized_audio_path: str,
    ) -> AudioChunk:
        chunk, _ = AudioChunk.objects.update_or_create(
            lecture=lecture,
            chunk_index=spec.chunk_index,
            defaults={
                "start_ts": spec.original_start,
                "end_ts": spec.original_end,
                "duration_seconds": round(spec.duration_seconds, 3),
                "original_audio_path": source_audio_path,
                "optimized_audio_path": optimized_audio_path,
            },
        )
        return chunk

    def _copy_original_if_needed(self, *, lecture: Lecture, audio_file: LectureFile) -> str:
        original_relative = self._temporary_storage_path(
            lecture=lecture,
            category="audio/original",
            filename=Path(audio_file.original_name).name,
        )
        if not self.storage.exists(original_relative):
            copied = self.storage.copy(source_path=audio_file.storage_path, destination_path=original_relative)
            StorageArtifactLifecycleService.register_temporary(
                user_id=str(lecture.user_id),
                lecture_id=str(lecture.id),
                storage_path=copied.path,
                size_bytes=copied.size,
                artifact_type="processing_audio_original",
            )
        return original_relative

    def _dispatch_incremental_transcription(self, *, lecture: Lecture, start_index: int | None = None) -> None:
        from apps.ai_pipeline.tasks import enqueue_transcription_batch
        from apps.audio_pipeline.providers.router_provider import RoutedTranscriptionProvider
        from apps.audio_pipeline.transcription.service import TranscriptionOrchestratorService

        lecture.refresh_from_db()
        if lecture.transcription_status in {TranscriptionStatus.NOT_STARTED, TranscriptionStatus.FAILED}:
            TranscriptionOrchestratorService(provider=RoutedTranscriptionProvider()).start(lecture=lecture)
            lecture.refresh_from_db()

        logger.info(
            "segmented_transcription_batch_enqueued",
            extra={
                "lecture_id": str(lecture.id),
                "start_index": start_index,
                "limit": self._segment_batch_size(),
                "audio_chunk_count": lecture.audio_chunks.count(),
            },
        )
        enqueue_transcription_batch(
            lecture_id=str(lecture.id),
            start_index=start_index,
            limit=self._segment_batch_size(),
        )

    def _run_whole_file_pipeline(self, *, lecture: Lecture, original_relative: str, original_path: str) -> dict[str, object]:
        normalized_path = self._build_output_path(lecture_id=str(lecture.id), category="normalized", filename="normalized.wav")
        optimized_path = self._build_output_path(lecture_id=str(lecture.id), category="optimized", filename="optimized.wav")
        self.ffmpeg.ensure_parent(file_path=normalized_path)
        self.ffmpeg.ensure_parent(file_path=optimized_path)
        normalization_filter = self._normalization_filter(fast_mode=bool(settings.AUDIO_FAST_NORMALIZATION))

        self.update_progress(
            lecture=lecture,
            stage="NORMALIZING",
            progress=10,
            extra={"audio_processing_mode": "whole_file", "audio_chunk_generation_complete": False},
        )
        self.ffmpeg.normalize(
            input_path=original_path,
            output_path=normalized_path,
            audio_filter=normalization_filter,
            threads=settings.AUDIO_FFMPEG_THREADS,
        )
        normalized_relative_path = self._temporary_storage_path(
            lecture=lecture,
            category="audio/normalized",
            filename="normalized.wav",
        )
        self._persist_output(
            lecture=lecture,
            local_path=normalized_path,
            relative_path=normalized_relative_path,
            content_type="audio/wav",
            artifact_type="normalized_audio",
        )
        original_duration = self.ffmpeg.probe_duration(input_path=normalized_path)

        self.update_progress(lecture=lecture, stage="CLEANING", progress=25)
        speech_segments, silence_segments, speech_ratio = self.vad.detect(wav_path=normalized_path)
        detected_speech_ratio = speech_ratio
        speech_duration = sum(segment.duration for segment in speech_segments)
        used_conservative_audio_fallback = False
        if original_duration <= 600 and (
            speech_ratio < self.min_speech_ratio_floor or speech_duration < max(20.0, original_duration * 0.5)
        ):
            speech_segments, silence_segments, speech_ratio = self._coarse_preserve_segments(
                speech_segments=speech_segments,
                total_duration=original_duration,
            )
            speech_duration = sum(segment.duration for segment in speech_segments)
            used_conservative_audio_fallback = True

        speech_segments, silence_segments, speech_ratio = self._preserve_short_pauses(
            speech_segments=speech_segments,
            total_duration=original_duration,
        )
        speech_duration = sum(segment.duration for segment in speech_segments)
        effective_speed_multiplier = self._effective_speed_multiplier(speech_duration=speech_duration)

        self.update_progress(lecture=lecture, stage="REMOVING_SILENCE", progress=45)
        self.ffmpeg.build_speech_only_audio(
            input_path=normalized_path,
            output_path=optimized_path,
            speech_segments=speech_segments,
            speed_multiplier=effective_speed_multiplier,
            threads=settings.AUDIO_FFMPEG_THREADS,
        )

        self.update_progress(lecture=lecture, stage="SPEED_OPTIMIZING", progress=60)
        mappings = self._speech_mappings(speech_segments=speech_segments, speed_multiplier=effective_speed_multiplier)
        expected_optimized_duration = round(speech_duration / effective_speed_multiplier, 3) if speech_duration > 0 else 0.0
        optimized_duration = self.ffmpeg.probe_duration(input_path=optimized_path)
        used_safe_speedup_fallback = False
        duration_mismatch = abs(optimized_duration - expected_optimized_duration)
        if expected_optimized_duration > 0 and duration_mismatch > max(2.0, expected_optimized_duration * 0.2):
            self.ffmpeg.build_speech_only_audio(
                input_path=normalized_path,
                output_path=optimized_path,
                speech_segments=speech_segments,
                speed_multiplier=1.0,
            )
            optimized_duration = self.ffmpeg.probe_duration(input_path=optimized_path)
            used_safe_speedup_fallback = True
            effective_speed_multiplier = 1.0
            mappings = self._speech_mappings(speech_segments=speech_segments, speed_multiplier=effective_speed_multiplier)
            expected_optimized_duration = round(speech_duration / effective_speed_multiplier, 3) if speech_duration > 0 else 0.0

        optimized_relative_path = self._temporary_storage_path(
            lecture=lecture,
            category="audio/optimized",
            filename="optimized.wav",
        )
        self._persist_output(
            lecture=lecture,
            local_path=optimized_path,
            relative_path=optimized_relative_path,
            content_type="audio/wav",
            artifact_type="optimized_audio",
        )

        target_chunk_seconds = max(
            self.transcription_min_chunk_seconds,
            min(self.transcription_target_chunk_seconds, self.transcription_max_chunk_seconds - 0.1),
        )
        chunk_specs = self.chunking.build_chunks(
            mappings=mappings,
            target_seconds=target_chunk_seconds,
            max_seconds=self.transcription_max_chunk_seconds,
            min_seconds=self.transcription_min_chunk_seconds,
            billing_unit_seconds=self.transcription_billing_unit_seconds,
        )

        self.update_progress(lecture=lecture, stage="CHUNKING", progress=75)
        chunk_records: list[dict[str, object]] = []
        for spec in chunk_specs:
            chunk_filename = f"chunk-{spec.chunk_index:03d}.mp3"
            chunk_path = self._build_output_path(lecture_id=str(lecture.id), category="chunks", filename=chunk_filename)
            self.ffmpeg.ensure_parent(file_path=chunk_path)
            self.ffmpeg.extract_chunk(
                input_path=optimized_path,
                output_path=chunk_path,
                start_ts=spec.optimized_start,
                end_ts=spec.optimized_end,
            )
            chunk_relative_path = self._temporary_storage_path(
                lecture=lecture,
                category="audio/chunks",
                filename=chunk_filename,
            )
            self._persist_output(
                lecture=lecture,
                local_path=chunk_path,
                relative_path=chunk_relative_path,
                content_type="audio/mpeg",
                artifact_type="audio_chunk",
            )
            chunk_records.append(
                {
                    "chunk_index": spec.chunk_index,
                    "optimized_audio_path": chunk_relative_path,
                    "optimized_start": spec.optimized_start,
                    "optimized_end": spec.optimized_end,
                    "original_start": spec.original_start,
                    "original_end": spec.original_end,
                    "duration_seconds": round(spec.duration_seconds, 3),
                    "estimated_billable_units": self.metadata.billable_units_for_duration(
                        duration_seconds=spec.duration_seconds,
                        billing_unit_seconds=self.transcription_billing_unit_seconds,
                    ),
                }
            )

        silence_removed_seconds = max(0.0, original_duration - speech_duration)
        estimated_reduction = self.metadata.estimated_cost_reduction_percent(
            original_duration=original_duration,
            optimized_duration=optimized_duration,
        )
        estimated_billable_units = self.metadata.billable_units_for_chunks(
            chunk_durations=[spec.duration_seconds for spec in chunk_specs],
            billing_unit_seconds=self.transcription_billing_unit_seconds,
        )
        ideal_billable_units = self.metadata.billable_units_for_duration(
            duration_seconds=optimized_duration,
            billing_unit_seconds=self.transcription_billing_unit_seconds,
        )
        waveform_preview = self.waveform.generate(wav_path=optimized_path)

        self.store_chunks(
            lecture=lecture,
            source_audio_path=original_relative,
            chunk_specs=chunk_specs,
            chunk_records=chunk_records,
        )

        result = AudioProcessingResult(
            original_duration=original_duration,
            optimized_duration=optimized_duration,
            silence_removed_seconds=silence_removed_seconds,
            speed_multiplier=effective_speed_multiplier,
            chunk_count=len(chunk_specs),
            estimated_cost_reduction_percent=estimated_reduction,
            speech_ratio=speech_ratio,
            normalized_audio_path=normalized_relative_path,
            optimized_audio_path=optimized_relative_path,
            processing_mode="whole_file",
            chunk_specs=chunk_specs,
            speech_segments=[{"start": segment.start, "end": segment.end} for segment in speech_segments],
            silence_segments=[{"start": segment.start, "end": segment.end} for segment in silence_segments],
            timestamp_mappings=[
                {
                    "original_start": mapping.original_start,
                    "original_end": mapping.original_end,
                    "optimized_start": mapping.optimized_start,
                    "optimized_end": mapping.optimized_end,
                }
                for mapping in mappings
            ],
            waveform_preview_data=waveform_preview,
            processing_steps=[
                {"stage": "NORMALIZING", "status": "completed"},
                {"stage": "CLEANING", "status": "completed"},
                {"stage": "REMOVING_SILENCE", "status": "completed"},
                {"stage": "SPEED_OPTIMIZING", "status": "completed"},
                {"stage": "CHUNKING", "status": "completed"},
                {"stage": "METADATA", "status": "completed"},
                {
                    "stage": "TRANSCRIPTION_CHUNK_POLICY",
                    "status": "completed",
                    "max_chunk_seconds": self.transcription_max_chunk_seconds,
                    "min_chunk_seconds": self.transcription_min_chunk_seconds,
                    "target_chunk_seconds": target_chunk_seconds,
                    "billing_unit_seconds": self.transcription_billing_unit_seconds,
                    "estimated_billable_units": estimated_billable_units,
                    "ideal_billable_units": ideal_billable_units,
                    "estimated_billing_unit_overhead": max(0, estimated_billable_units - ideal_billable_units),
                },
                {
                    "stage": "SILENCE_REMOVAL_SAFETY",
                    "status": "completed",
                    "expected_optimized_duration": expected_optimized_duration,
                    "actual_optimized_duration": optimized_duration,
                    "used_safe_speedup_fallback": used_safe_speedup_fallback,
                    "effective_speed_multiplier": effective_speed_multiplier,
                    "detected_speech_ratio": detected_speech_ratio,
                    "used_conservative_audio_fallback": used_conservative_audio_fallback,
                    "min_removable_silence_seconds": self.min_removable_silence_seconds,
                },
            ],
        )
        self.complete_pipeline(lecture=lecture, result=result)
        return AudioProcessingDebugService.debug_payload(lecture=lecture)

    def _run_segmented_audio_pipeline(self, *, lecture: Lecture, original_relative: str, original_path: str, original_duration: float) -> dict[str, object]:
        AudioChunk.objects.filter(lecture=lecture).delete()
        self._set_chunk_generation_complete(lecture=lecture, complete=False)
        normalization_filter = self._normalization_filter(fast_mode=True)
        segment_specs = self._build_segment_chunk_specs(duration_seconds=original_duration)
        total_segments = max(len(segment_specs), 1)
        target_chunk_seconds = self._target_chunk_seconds()
        produced_since_dispatch = 0
        next_dispatch_start: int | None = None
        chunk_records: list[dict[str, object]] = []
        final_chunk_specs: list[AudioChunkSpec] = []
        global_speech_segments: list[dict[str, float]] = []
        global_silence_segments: list[dict[str, float]] = []
        global_timestamp_mappings: list[dict[str, float]] = []
        temp_paths: list[str] = []
        cumulative_optimized_cursor = 0.0
        total_speech_duration = 0.0
        total_optimized_duration = 0.0
        total_silence_removed_seconds = 0.0
        weighted_speed_numerator = 0.0
        processing_steps: list[dict[str, object]] = [
            {"stage": "NORMALIZING", "status": "completed", "mode": "segmented_fast"},
            {"stage": "CLEANING", "status": "completed", "mode": "segmented_fast"},
            {"stage": "REMOVING_SILENCE", "status": "completed", "mode": "segmented_fast"},
            {"stage": "SPEED_OPTIMIZING", "status": "completed", "mode": "segmented_fast"},
            {"stage": "CHUNKING", "status": "in_progress", "mode": "segmented_fast"},
        ]
        pending_group: list[tuple[AudioChunkSpec, SegmentOptimizationResult]] = []
        pending_group_duration = 0.0
        next_final_chunk_index = 0

        self.update_progress(
            lecture=lecture,
            stage="NORMALIZING",
            progress=5,
            extra={
                "audio_processing_mode": "segmented",
                "audio_chunk_generation_complete": False,
                "chunk_count": total_segments,
            },
        )

        def flush_pending_group() -> None:
            nonlocal pending_group, pending_group_duration
            nonlocal produced_since_dispatch, next_dispatch_start, cumulative_optimized_cursor, next_final_chunk_index
            if not pending_group:
                return
            final_spec, chunk_record, speech_segments, silence_segments, timestamp_mappings = self._finalize_segment_group(
                lecture=lecture,
                source_audio_path=original_relative,
                group=pending_group,
                chunk_index=next_final_chunk_index,
                optimized_start=cumulative_optimized_cursor,
            )
            final_chunk_specs.append(final_spec)
            chunk_records.append(chunk_record)
            global_speech_segments.extend(speech_segments)
            global_silence_segments.extend(silence_segments)
            global_timestamp_mappings.extend(timestamp_mappings)
            cumulative_optimized_cursor = final_spec.optimized_end
            produced_since_dispatch += 1
            if next_dispatch_start is None:
                next_dispatch_start = final_spec.chunk_index
            next_final_chunk_index += 1
            if settings.AUDIO_DELETE_TEMP_SEGMENTS_AFTER_SUCCESS:
                self._cleanup_temp_paths(paths=[result.chunk_local_path for _, result in pending_group])
            pending_group = []
            pending_group_duration = 0.0

        for spec in segment_specs:
            logger.info(
                "audio_segment_transcoding_started",
                extra={
                    "lecture_id": str(lecture.id),
                    "chunk_index": spec.chunk_index,
                    "start_ts": spec.original_start,
                    "end_ts": spec.original_end,
                },
            )
            segment_result = self._optimize_segment_chunk(
                lecture=lecture,
                original_path=original_path,
                spec=spec,
                normalization_filter=normalization_filter,
            )
            temp_paths.extend(segment_result.temp_paths)
            total_speech_duration += segment_result.speech_duration
            total_optimized_duration += segment_result.optimized_duration
            total_silence_removed_seconds += segment_result.silence_removed_seconds
            weighted_speed_numerator += segment_result.speed_multiplier * max(segment_result.speech_duration, 0.0)
            pending_group.append((spec, segment_result))
            pending_group_duration = round(pending_group_duration + segment_result.optimized_duration, 3)
            progress = min(95, 10 + round(((spec.chunk_index + 1) / total_segments) * 80))
            if len(pending_group) > 1 and pending_group_duration > self.transcription_max_chunk_seconds:
                overflow_item = pending_group.pop()
                pending_group_duration = round(sum(result.optimized_duration for _, result in pending_group), 3)
                flush_pending_group()
                pending_group.append(overflow_item)
                pending_group_duration = round(sum(result.optimized_duration for _, result in pending_group), 3)
            if pending_group_duration >= target_chunk_seconds:
                flush_pending_group()
            self.update_progress(
                lecture=lecture,
                stage="CHUNKING",
                progress=progress,
                extra={
                    "audio_processing_mode": "segmented",
                    "audio_chunk_generation_complete": False,
                    "chunk_count": len(final_chunk_specs) + len(pending_group),
                    "chunks": chunk_records,
                    "processing_steps": processing_steps,
                    "normalized_audio_path": "",
                    "optimized_audio_path": "",
                },
            )
            logger.info(
                "audio_segment_transcoding_completed",
                extra={
                    "lecture_id": str(lecture.id),
                    "chunk_index": spec.chunk_index,
                    "duration_seconds": round(segment_result.optimized_duration, 3),
                    "speech_ratio": segment_result.speech_ratio,
                    "silence_removed_seconds": segment_result.silence_removed_seconds,
                    "speed_multiplier": segment_result.speed_multiplier,
                    "output_path": segment_result.chunk_relative_path,
                },
            )
            if produced_since_dispatch >= self._segment_batch_size():
                self._dispatch_incremental_transcription(lecture=lecture, start_index=next_dispatch_start)
                produced_since_dispatch = 0
                next_dispatch_start = None

        flush_pending_group()

        if next_dispatch_start is not None:
            self._dispatch_incremental_transcription(lecture=lecture, start_index=next_dispatch_start)

        estimated_billable_units = self.metadata.billable_units_for_chunks(
            chunk_durations=[spec.duration_seconds for spec in final_chunk_specs],
            billing_unit_seconds=self.transcription_billing_unit_seconds,
        )
        ideal_billable_units = self.metadata.billable_units_for_duration(
            duration_seconds=total_optimized_duration,
            billing_unit_seconds=self.transcription_billing_unit_seconds,
        )
        speech_ratio = round(total_speech_duration / original_duration, 4) if original_duration > 0 else 0.0
        effective_speed_multiplier = (
            round(weighted_speed_numerator / total_speech_duration, 3) if total_speech_duration > 0 else 1.0
        )
        estimated_reduction = self.metadata.estimated_cost_reduction_percent(
            original_duration=original_duration,
            optimized_duration=total_optimized_duration or original_duration,
        )
        processing_steps.extend(
            [
                {"stage": "CHUNKING", "status": "completed", "mode": "segmented_fast"},
                {
                    "stage": "TRANSCRIPTION_CHUNK_POLICY",
                    "status": "completed",
                    "max_chunk_seconds": self.transcription_max_chunk_seconds,
                    "min_chunk_seconds": self.transcription_min_chunk_seconds,
                    "target_chunk_seconds": target_chunk_seconds,
                    "billing_unit_seconds": self.transcription_billing_unit_seconds,
                    "estimated_billable_units": estimated_billable_units,
                    "ideal_billable_units": ideal_billable_units,
                    "estimated_billing_unit_overhead": max(0, estimated_billable_units - ideal_billable_units),
                },
            ]
        )
        result = AudioProcessingResult(
            original_duration=original_duration,
            optimized_duration=round(total_optimized_duration or original_duration, 3),
            silence_removed_seconds=round(total_silence_removed_seconds, 3),
            speed_multiplier=effective_speed_multiplier,
            chunk_count=len(final_chunk_specs),
            estimated_cost_reduction_percent=estimated_reduction,
            speech_ratio=speech_ratio,
            normalized_audio_path="",
            optimized_audio_path="",
            processing_mode="segmented",
            temp_paths=temp_paths,
            chunk_specs=final_chunk_specs,
            speech_segments=global_speech_segments,
            silence_segments=global_silence_segments,
            timestamp_mappings=global_timestamp_mappings,
            waveform_preview_data=[],
            processing_steps=processing_steps,
        )
        if settings.AUDIO_DELETE_TEMP_SEGMENTS_AFTER_SUCCESS:
            self._cleanup_temp_paths(paths=temp_paths)
        self.complete_pipeline(lecture=lecture, result=result)
        return AudioProcessingDebugService.debug_payload(lecture=lecture)

    def run_pipeline(self, *, lecture: Lecture) -> dict[str, object]:
        audio_file = self.get_audio_file(lecture=lecture)
        with measure_scaling_stage(
            stage="audio_preprocessing",
            lecture_id=str(lecture.id),
            input_size_bytes=audio_file.file_size,
            audio_duration_seconds=audio_file.duration_seconds,
        ) as measurement:
            self.start_processing(lecture=lecture)
            original_relative = self._copy_original_if_needed(lecture=lecture, audio_file=audio_file)
            with tempfile.TemporaryDirectory(prefix="tesanote-audio-") as workspace:
                self._working_directory = Path(workspace)
                try:
                    with self.storage.materialize_to_temp(original_relative) as original_path:
                        original_duration = self.ffmpeg.probe_duration(input_path=original_path)
                        processing_mode = "segmented" if self._should_use_segmented_pipeline(duration_seconds=original_duration) else "whole_file"
                        measurement.add(
                            audio_duration_seconds=round(original_duration, 3),
                            processing_mode=processing_mode,
                        )
                        logger.info(
                            "audio_pipeline_mode_selected",
                            extra={
                                "lecture_id": str(lecture.id),
                                "original_duration": round(original_duration, 3),
                                "mode": processing_mode,
                                "fast_normalization": bool(settings.AUDIO_FAST_NORMALIZATION),
                                "ffmpeg_threads": settings.AUDIO_FFMPEG_THREADS,
                            },
                        )
                        if processing_mode == "segmented":
                            result = self._run_segmented_audio_pipeline(
                                lecture=lecture,
                                original_relative=original_relative,
                                original_path=original_path,
                                original_duration=original_duration,
                            )
                        else:
                            result = self._run_whole_file_pipeline(
                                lecture=lecture,
                                original_relative=original_relative,
                                original_path=original_path,
                            )
                finally:
                    self._working_directory = None
            measurement.add(chunk_count=lecture.audio_chunks.count())
            return result
