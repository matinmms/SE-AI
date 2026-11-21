from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from typing import Iterator

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

logger = logging.getLogger("tesanote")

PROBE_TIMEOUT_SECONDS = 60
SCAN_TIMEOUT_SECONDS = 45
_TIME_PATTERN = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")


def _positive_seconds(value: object) -> float | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds <= 0:
        return None
    return seconds


def _existing_path(file_obj: UploadedFile) -> str | None:
    getter = getattr(file_obj, "temporary_file_path", None)
    if callable(getter):
        try:
            path = getter()
            if path and os.path.isfile(path):
                return path
        except Exception:
            pass
    inner_name = getattr(getattr(file_obj, "file", None), "name", None)
    if isinstance(inner_name, str) and os.path.isfile(inner_name):
        return inner_name
    return None


@contextmanager
def _real_file_path(file_obj: UploadedFile) -> Iterator[str]:
    """Yield a filesystem path for the upload, spooling to a temp file only when needed."""
    existing = _existing_path(file_obj)
    if existing:
        yield existing
        return

    suffix = os.path.splitext(getattr(file_obj, "name", "") or "")[1] or ".bin"
    handle = tempfile.NamedTemporaryFile(prefix="duration-probe-", suffix=suffix, delete=False)
    try:
        file_obj.seek(0)
        shutil.copyfileobj(file_obj, handle, length=1024 * 1024)
        handle.close()
        yield handle.name
    finally:
        handle.close()
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        file_obj.seek(0)


def _ffprobe_duration(path: str) -> float | None:
    binary = getattr(settings, "FFPROBE_BINARY", "ffprobe")
    if shutil.which(binary) is None:
        return None
    try:
        result = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format=duration:stream=duration",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        return None
    candidates = [_positive_seconds((payload.get("format") or {}).get("duration"))]
    candidates += [_positive_seconds(stream.get("duration")) for stream in payload.get("streams") or []]
    values = [value for value in candidates if value is not None]
    return max(values) if values else None


def _ffmpeg_scan_duration(path: str) -> float | None:
    """Decode-free pass over the packets; the last reported timestamp is the real length."""
    binary = getattr(settings, "FFMPEG_BINARY", "ffmpeg")
    if shutil.which(binary) is None:
        return None
    try:
        result = subprocess.run(
            [binary, "-v", "error", "-stats", "-i", path, "-vn", "-c", "copy", "-f", "null", "-"],
            capture_output=True,
            text=True,
            check=False,
            timeout=SCAN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    matches = _TIME_PATTERN.findall(result.stderr or "")
    if not matches:
        return None
    hours, minutes, seconds = matches[-1]
    return _positive_seconds(int(hours) * 3600 + int(minutes) * 60 + float(seconds))


def probe_audio_duration_seconds(file_obj: UploadedFile) -> float | None:
    """Authoritative duration for containers whose header metadata is missing or zero."""
    try:
        with _real_file_path(file_obj) as path:
            return _ffprobe_duration(path) or _ffmpeg_scan_duration(path)
    except Exception:
        logger.warning("audio duration probe failed", exc_info=True)
        return None
