from __future__ import annotations

import math
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from django.conf import settings

from apps.audio_pipeline.processors.ffmpeg import FFmpegProcessor
from services.transcription.audio_utils import SUPPORTED_AUDIO_EXTENSIONS, validate_audio_file
from services.transcription.exceptions import TranscriptionValidationError


@dataclass
class PreparedTranscriptionPart:
    chunk_index: int
    path: str
    duration_seconds: float
    audio_size_bytes: int
    start_ts: float
    end_ts: float


class TranscriptionChunkPreparer:
    def __init__(self) -> None:
        self.ffmpeg = FFmpegProcessor(ffmpeg_binary=settings.FFMPEG_BINARY, ffprobe_binary=settings.FFPROBE_BINARY)
        self.max_file_bytes = int(settings.MAX_AVALAI_STT_FILE_MB * 1024 * 1024)
        self.target_file_bytes = int(settings.TRANSCRIPTION_CHUNK_TARGET_MB * 1024 * 1024)
        self.max_chunk_seconds = int(settings.TRANSCRIPTION_CHUNK_MAX_SECONDS)

    @contextmanager
    def prepare(self, *, audio_path: str) -> Iterator[list[PreparedTranscriptionPart]]:
        source_path = Path(audio_path)
        duration_seconds = validate_audio_file(audio_path=audio_path, ffmpeg=self.ffmpeg)
        source_size = source_path.stat().st_size
        extension = source_path.suffix.lower()

        if extension in SUPPORTED_AUDIO_EXTENSIONS and source_size <= self.max_file_bytes:
            yield [
                PreparedTranscriptionPart(
                    chunk_index=0,
                    path=str(source_path),
                    duration_seconds=duration_seconds,
                    audio_size_bytes=source_size,
                    start_ts=0.0,
                    end_ts=duration_seconds,
                )
            ]
            return

        with tempfile.TemporaryDirectory(prefix="tesanote-avalai-stt-") as tmp_dir:
            yield self._build_segmented_parts(
                source_path=str(source_path),
                total_duration=duration_seconds,
                output_dir=tmp_dir,
            )

    def _build_segmented_parts(
        self,
        *,
        source_path: str,
        total_duration: float,
        output_dir: str,
    ) -> list[PreparedTranscriptionPart]:
        segment_seconds = max(60, min(self.max_chunk_seconds, int(math.ceil(total_duration)) or self.max_chunk_seconds))
        part_count = max(1, int(math.ceil(total_duration / segment_seconds)))
        parts: list[PreparedTranscriptionPart] = []
        start_ts = 0.0

        for chunk_index in range(part_count):
            remaining = max(0.0, total_duration - start_ts)
            duration = min(segment_seconds, remaining)
            if duration <= 0:
                break
            output_path = Path(output_dir) / f"transcription-part-{chunk_index:03d}.mp3"
            self.ffmpeg.transcode_segment_to_transcription_mp3(
                input_path=source_path,
                output_path=str(output_path),
                start_ts=start_ts,
                duration_seconds=duration,
                bitrate_kbps=64,
            )
            size_bytes = output_path.stat().st_size
            if size_bytes > self.max_file_bytes:
                parts.extend(
                    self._split_oversized_part(
                        source_path=source_path,
                        output_dir=output_dir,
                        chunk_index=chunk_index,
                        start_ts=start_ts,
                        duration_seconds=duration,
                    )
                )
            else:
                parts.append(
                    PreparedTranscriptionPart(
                        chunk_index=len(parts),
                        path=str(output_path),
                        duration_seconds=duration,
                        audio_size_bytes=size_bytes,
                        start_ts=round(start_ts, 3),
                        end_ts=round(start_ts + duration, 3),
                    )
                )
            start_ts += duration

        if not parts:
            raise TranscriptionValidationError(message="Audio could not be prepared for transcription.")
        return parts

    def _split_oversized_part(
        self,
        *,
        source_path: str,
        output_dir: str,
        chunk_index: int,
        start_ts: float,
        duration_seconds: float,
    ) -> list[PreparedTranscriptionPart]:
        if duration_seconds <= 30:
            raise TranscriptionValidationError(message="Audio chunk is too large for AvalAI transcription.")

        half_duration = duration_seconds / 2.0
        parts: list[PreparedTranscriptionPart] = []
        for offset_index, offset_start in enumerate((start_ts, start_ts + half_duration)):
            output_path = Path(output_dir) / f"transcription-part-{chunk_index:03d}-{offset_index}.mp3"
            self.ffmpeg.transcode_segment_to_transcription_mp3(
                input_path=source_path,
                output_path=str(output_path),
                start_ts=offset_start,
                duration_seconds=half_duration,
                bitrate_kbps=64,
            )
            size_bytes = output_path.stat().st_size
            if size_bytes > self.max_file_bytes:
                parts.extend(
                    self._split_oversized_part(
                        source_path=source_path,
                        output_dir=output_dir,
                        chunk_index=(chunk_index * 10) + offset_index + 1,
                        start_ts=offset_start,
                        duration_seconds=half_duration,
                    )
                )
            else:
                parts.append(
                    PreparedTranscriptionPart(
                        chunk_index=0,
                        path=str(output_path),
                        duration_seconds=half_duration,
                        audio_size_bytes=size_bytes,
                        start_ts=round(offset_start, 3),
                        end_ts=round(offset_start + half_duration, 3),
                    )
                )
        for index, part in enumerate(parts):
            part.chunk_index = index
        return parts
