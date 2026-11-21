"""Persist speech-to-text segments on the lecture's absolute timeline."""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from apps.audio_pipeline.models import AudioChunk, TranscriptSegment

logger = logging.getLogger("tesanote")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def has_recognizer_stats(segments: list[dict[str, Any]] | None) -> bool:
    return any(
        isinstance(segment, dict) and ("avg_logprob" in segment or "no_speech_prob" in segment)
        for segment in (segments or [])
    )


def persist_segments(*, audio_chunk: AudioChunk, segments: list[dict[str, Any]]) -> int:
    """Replace the chunk's segments. Never raises: the transcript must not fail because of this."""
    try:
        rows: list[TranscriptSegment] = []
        window_ids: dict[Any, int] = {}
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                continue
            start = _number(segment.get("start"))
            end = _number(segment.get("end"))
            if start is None or end is None or end < start:
                continue
            window_key = segment.get("window", segment.get("seek", 0))
            window_index = window_ids.setdefault(window_key, len(window_ids))
            rows.append(
                TranscriptSegment(
                    lecture_id=audio_chunk.lecture_id,
                    audio_chunk=audio_chunk,
                    segment_index=index,
                    window_index=window_index,
                    # Computed once, here. The segment's time is relative to the file sent to the
                    # recognizer (the router already added the part offset); the chunk start is
                    # the file's position in the lecture.
                    start_abs=round(float(audio_chunk.start_ts) + start, 3),
                    end_abs=round(float(audio_chunk.start_ts) + end, 3),
                    text=str(segment.get("text") or "").strip()[:2000],
                    avg_logprob=_number(segment.get("avg_logprob")),
                    no_speech_prob=_number(segment.get("no_speech_prob")),
                    compression_ratio=_number(segment.get("compression_ratio")),
                )
            )
        with transaction.atomic():
            TranscriptSegment.objects.filter(audio_chunk=audio_chunk).delete()
            TranscriptSegment.objects.bulk_create(rows)
        return len(rows)
    except Exception:
        logger.exception(
            "note_review_segment_capture_failed",
            extra={"audio_chunk_id": str(getattr(audio_chunk, "id", ""))},
        )
        return 0
