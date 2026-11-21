"""Compute and persist the accuracy check for one note, and cut the audio clips.

Idempotent: recomputing replaces the previous review and its clips. Every failure degrades to an
UNAVAILABLE review (the chip is simply not shown); nothing here may break note generation.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.db import transaction

from apps.audio_pipeline.models import AudioChunk, TranscriptSegment
from apps.notes.models import GeneratedNote, NoteReview, NoteReviewSpot, NoteReviewState
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from infrastructure.storage.factory import get_storage_service
from services.note_review.analysis import SegmentInfo, Spot, analyze, clear_ratio, extract_sections
from services.note_review.eligibility import capture_enabled_for_user_id
from services.note_review.timeline import Timeline, build_timeline

logger = logging.getLogger("tesanote")

ALGORITHM_VERSION = 3
CLIP_CONTENT_TYPE = "audio/mp4"


def _ffmpeg() -> str:
    return str(settings.FFMPEG_BINARY or "ffmpeg")


def _ffprobe() -> str:
    return str(settings.FFPROBE_BINARY or "ffprobe")


def check_timeline(chunks: list[AudioChunk], *, lecture_duration: float | None, tolerance: float) -> tuple[bool, dict]:
    """The audio-moment feature is only trusted when the chunk timeline is exact.

    Chunks must be contiguous and each chunk's length must match its span; the end of the last
    chunk must match the known audio duration. Any mismatch disables the feature for the lecture.
    """
    details: dict = {"chunks": len(chunks), "tolerance_s": tolerance}
    if not chunks:
        return False, {**details, "problem": "no_chunks"}
    ordered = sorted(chunks, key=lambda chunk: chunk.chunk_index)
    for previous, current in zip(ordered, ordered[1:]):
        gap = abs(float(current.start_ts) - float(previous.end_ts))
        if gap > tolerance:
            return False, {**details, "problem": "gap_between_chunks", "gap_s": round(gap, 3), "after_chunk": previous.chunk_index}
    for chunk in ordered:
        span = float(chunk.end_ts) - float(chunk.start_ts)
        if abs(span - float(chunk.duration_seconds)) > tolerance:
            return False, {**details, "problem": "chunk_span_mismatch", "chunk": chunk.chunk_index}
    last_end = float(ordered[-1].end_ts)
    details["audio_end_s"] = round(last_end, 3)
    if lecture_duration:
        difference = abs(last_end - float(lecture_duration))
        details["duration_difference_s"] = round(difference, 3)
        if difference > max(tolerance, 0.5):
            return False, {**details, "problem": "duration_mismatch"}
    return True, details


def _probe_duration(path: str) -> float | None:
    try:
        result = subprocess.run(
            [_ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _cut_to_wav(*, source: str, start: float, duration: float, target: str) -> bool:
    command = [_ffmpeg(), "-v", "error", "-y", "-ss", f"{max(0.0, start):.3f}", "-t", f"{duration:.3f}", "-i", source,
               "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", target]
    try:
        subprocess.run(command, capture_output=True, timeout=120, check=True)
        return os.path.getsize(target) > 0
    except Exception:
        return False


def cut_clip(
    *, chunks: list[AudioChunk], start: float, end: float, destination: str, timeline: Timeline | None = None, speed: float = 1.0
) -> bool:
    """Cut [start, end] of the processed timeline from the stored chunk files into one AAC/M4A file.

    The chunk files hold sped-up audio; `speed` slows the finished clip back to natural speed so
    the user hears the lecturer as spoken.
    """
    storage = get_storage_service()
    tolerance = float(settings.NOTE_REVIEW_DURATION_TOLERANCE_SECONDS)
    pieces: list[str] = []
    with tempfile.TemporaryDirectory(prefix="note-review-") as workdir:
        for index, chunk in enumerate(sorted(chunks, key=lambda item: item.chunk_index)):
            chunk_start, chunk_end = timeline.chunk_span(chunk) if timeline else (float(chunk.start_ts), float(chunk.end_ts))
            low, high = max(start, chunk_start), min(end, chunk_end)
            if high - low <= 0.05:
                continue
            try:
                with storage.materialize_to_temp(chunk.optimized_audio_path) as path:
                    actual = _probe_duration(path)
                    # A transcoded copy whose length differs from the chunk span would shift every
                    # time inside it. Refuse rather than play the wrong moment.
                    if actual is None or abs(actual - float(chunk.duration_seconds)) > max(tolerance, 0.3):
                        logger.warning("note_review_chunk_duration_drift", extra={"chunk": chunk.chunk_index})
                        return False
                    piece = os.path.join(workdir, f"piece-{index}.wav")
                    if not _cut_to_wav(source=path, start=low - chunk_start, duration=high - low, target=piece):
                        return False
                    pieces.append(piece)
            except Exception:
                logger.exception("note_review_chunk_unavailable", extra={"chunk": chunk.chunk_index})
                return False
        if not pieces:
            return False
        if len(pieces) == 1:
            source = pieces[0]
            concat_args = ["-i", source]
        else:
            listing = os.path.join(workdir, "pieces.txt")
            Path(listing).write_text("".join(f"file '{piece}'\n" for piece in pieces), encoding="utf-8")
            concat_args = ["-f", "concat", "-safe", "0", "-i", listing]
        slow = ["-af", f"atempo={max(0.5, 1.0 / speed):.4f}"] if speed > 1.01 else []
        command = [_ffmpeg(), "-v", "error", "-y", *concat_args, "-vn", "-ac", "1", *slow, "-c:a", "aac",
                   "-b:a", f"{int(settings.NOTE_REVIEW_CLIP_BITRATE_KBPS)}k", "-movflags", "+faststart", destination]
        try:
            subprocess.run(command, capture_output=True, timeout=120, check=True)
            return os.path.getsize(destination) > 0
        except Exception:
            logger.exception("note_review_clip_encode_failed")
            return False


def clip_window(spot: Spot, speed: float = 1.0) -> tuple[float, float]:
    """Clip bounds on the processed clock. Lead and maximum are natural-speed seconds."""
    speed = max(1.0, speed)
    lead = float(settings.NOTE_REVIEW_CLIP_LEAD_SECONDS) / speed
    maximum = float(settings.NOTE_REVIEW_CLIP_SECONDS) / speed
    start = max(0.0, spot.unit.start - lead)
    end = min(spot.unit.end + 1.0 / speed, start + maximum)
    return start, end


def delete_review(note_id) -> None:
    """Remove the review; the post_delete signal on spots deletes their stored clips."""
    NoteReview.objects.filter(note_id=note_id).delete()


def compute_note_review(note_id) -> NoteReview | None:
    note = GeneratedNote.objects.select_related("lecture").filter(id=note_id).first()
    if note is None:
        return None
    if not capture_enabled_for_user_id(note.user_id):
        return None
    lecture = note.lecture

    delete_review(note.id)

    def unavailable(reason: str, **details) -> NoteReview:
        return NoteReview.objects.create(
            note=note, user_id=note.user_id, state=NoteReviewState.UNAVAILABLE, reason=reason,
            algorithm_version=ALGORITHM_VERSION, details=details,
        )

    segment_rows = list(TranscriptSegment.objects.filter(lecture=lecture).order_by("start_abs", "segment_index"))
    if not segment_rows:
        return unavailable("no_segments")
    chunks = list(AudioChunk.objects.filter(lecture=lecture))
    tolerance = float(settings.NOTE_REVIEW_DURATION_TOLERANCE_SECONDS)
    metadata = lecture.processing_metadata if isinstance(lecture.processing_metadata, dict) else {}
    timeline, timeline_details = build_timeline(
        chunks,
        raw_mappings=metadata.get("timestamp_mappings"),
        processed_duration=lecture.optimized_audio_duration,
        tolerance=tolerance,
    )
    if timeline is None and timeline_details.get("mode") == "identity":
        # Nothing was sped up or cut: chunk time is lecture time, and it must be exact.
        identity_ok, timeline_details = check_timeline(
            chunks, lecture_duration=lecture.optimized_audio_duration or lecture.duration_seconds, tolerance=tolerance
        )
        timeline_details["mode"] = "identity"
        if identity_ok:
            timeline = Timeline(mode="identity", chunk_offsets={})
    if timeline is None:
        return unavailable("timeline_mismatch", timeline=timeline_details)
    sections = extract_sections(note.semantic_payload)
    if not sections:
        return unavailable("no_sections")

    chunk_by_id = {chunk.id: chunk for chunk in chunks}

    def processed_time(row, value: float) -> float:
        # Stored as chunk start + time inside the chunk file; recover the in-file time.
        chunk = chunk_by_id[row.audio_chunk_id]
        return timeline.to_processed(chunk, value - float(chunk.start_ts))

    segment_rows = [row for row in segment_rows if row.audio_chunk_id in chunk_by_id]
    if not segment_rows:
        return unavailable("no_segments")
    segments = [
        SegmentInfo(
            index=position, window_index=row.window_index, start=processed_time(row, row.start_abs),
            end=processed_time(row, row.end_abs), text=row.text,
            avg_logprob=row.avg_logprob, no_speech_prob=row.no_speech_prob, compression_ratio=row.compression_ratio,
        )
        for position, row in enumerate(segment_rows)
    ]
    min_badness = float(settings.NOTE_REVIEW_MIN_BADNESS)
    spots, units, stats = analyze(
        segments, sections,
        min_badness=min_badness,
        min_alignment=float(settings.NOTE_REVIEW_MIN_ALIGNMENT_SCORE),
        max_spots=int(settings.NOTE_REVIEW_MAX_SPOTS),
        max_ratio=float(settings.NOTE_REVIEW_MAX_FLAG_RATIO),
    )

    with transaction.atomic():
        # Not READY yet: the clips are cut below, and a summary read in between would show every
        # spot without its clip as "sound gone". The page sees "pending" and asks again.
        review = NoteReview.objects.create(
            note=note, user_id=note.user_id, state=NoteReviewState.UNAVAILABLE, reason="pending", flagged_count=len(spots),
            clear_ratio=clear_ratio(units, min_badness=min_badness), timeline_ok=True,
            algorithm_version=ALGORITHM_VERSION, details={"timeline": timeline_details, **stats},
        )
        created: list[NoteReviewSpot] = []
        for order, spot in enumerate(spots):
            speed = timeline.speed_at(spot.unit.start)
            clip_start, clip_end = clip_window(spot, speed)
            lecture_start = timeline.to_lecture(spot.unit.start)
            created.append(
                NoteReviewSpot.objects.create(
                    review=review, note=note, order=order, section_index=spot.section_index,
                    heading_text=sections[spot.section_index].heading[:255], start_s=round(lecture_start, 3),
                    end_s=round(timeline.to_lecture(spot.unit.end), 3), badness=round(spot.badness, 4), alignment_score=round(spot.alignment, 4),
                    reason_codes=spot.reasons, snippet=spot.unit.text[:300], section_fraction=spot.section_fraction,
                    # Natural-speed clock: the passage sits (its lead-in) after the clip start.
                    clip_start_s=round(lecture_start - (spot.unit.start - clip_start) * speed, 3),
                    clip_duration_s=round((clip_end - clip_start) * speed, 3),
                )
            )

    try:
        _store_clips(note=note, chunks=chunks, timeline=timeline, created=created, spots=spots)
    finally:
        # Ready even if a clip failed: that spot still shows its lecture time.
        review.state = NoteReviewState.READY
        review.reason = ""
        review.save(update_fields=["state", "reason", "updated_at"])
    return review


def _store_clips(*, note, chunks, timeline, created, spots) -> None:
    # Clips are best effort and outside the transaction: a missing clip leaves the spot with its time.
    storage = get_storage_service()
    for spot_row, spot in zip(created, spots):
        speed = timeline.speed_at(spot.unit.start)
        clip_start, clip_end = clip_window(spot, speed)
        storage_path = f"notes/{note.id}/review/{spot_row.id}.m4a"
        with tempfile.TemporaryDirectory(prefix="note-review-out-") as outdir:
            target = os.path.join(outdir, "clip.m4a")
            if not cut_clip(chunks=chunks, start=clip_start, end=clip_end, destination=target, timeline=timeline, speed=speed):
                continue
            try:
                stored = storage.save_file(relative_path=storage_path, local_path=target, content_type=CLIP_CONTENT_TYPE)
            except Exception:
                logger.exception("note_review_clip_store_failed", extra={"note_id": str(note.id)})
                continue
        StorageArtifactLifecycleService.register_persistent(
            user_id=str(note.user_id), lecture_id=str(note.lecture_id), storage_path=stored.path,
            size_bytes=stored.size, artifact_type="note_review_clip",
        )
        spot_row.clip_path = stored.path
        spot_row.clip_bytes = int(stored.size)
        spot_row.save(update_fields=["clip_path", "clip_bytes", "updated_at"])
