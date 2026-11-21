from __future__ import annotations

from pathlib import Path

from django.conf import settings

from apps.audio_pipeline.processors.ffmpeg import FFmpegProcessor
from services.transcription.exceptions import TranscriptionValidationError

SUPPORTED_AUDIO_EXTENSIONS = {
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".mpeg",
    ".mpga",
    ".ogg",
    ".wav",
    ".webm",
}


def max_audio_duration_seconds() -> int:
    return int(settings.MAX_AUDIO_DURATION_MINUTES * 60)


def validate_audio_file(*, audio_path: str, ffmpeg: FFmpegProcessor) -> float:
    path = Path(audio_path)
    if not path.exists() or not path.is_file():
        raise TranscriptionValidationError(message="Audio file is missing.")

    duration_seconds = ffmpeg.probe_duration(input_path=str(path))
    if duration_seconds <= 0:
        raise TranscriptionValidationError(message="Audio duration could not be detected.")
    if duration_seconds > max_audio_duration_seconds():
        raise TranscriptionValidationError(message="مدت زمان فایل صوتی نباید بیشتر از ۹۰ دقیقه باشد.")
    return duration_seconds
