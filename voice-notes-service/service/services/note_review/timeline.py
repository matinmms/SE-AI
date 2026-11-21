"""Maps recognizer time back to lecture time.

Audio is sped up and its silences are cut before it is transcribed, so a segment time is a
position in the *processed* audio, not in the lecture. The processing step records exactly how
every processed stretch maps back (`processing_metadata["timestamp_mappings"]`); this module
uses that record instead of assuming the two clocks are equal.

Two modes:
- mapped: chunk files are laid end to end on the processed clock (chunk offset = sum of the
  durations before it) and the recorded mappings convert processed time to lecture time.
- identity: nothing was sped up or cut (no mappings recorded); chunk time is lecture time.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Any, Iterable


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


@dataclass
class Piece:
    optimized_start: float
    optimized_end: float
    original_start: float
    original_end: float

    @property
    def optimized_length(self) -> float:
        return self.optimized_end - self.optimized_start

    @property
    def speed(self) -> float:
        if self.optimized_length <= 0:
            return 1.0
        return max(1.0, (self.original_end - self.original_start) / self.optimized_length)


@dataclass
class Timeline:
    mode: str  # "mapped" | "identity"
    chunk_offsets: dict[int, float]  # chunk_index -> start on the processed clock
    pieces: list[Piece] = field(default_factory=list)
    _starts: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._starts = [piece.optimized_start for piece in self.pieces]

    def chunk_span(self, chunk) -> tuple[float, float]:
        """Start and end of a chunk file on the processed clock."""
        if self.mode == "identity":
            return float(chunk.start_ts), float(chunk.end_ts)
        offset = self.chunk_offsets[chunk.chunk_index]
        return offset, offset + float(chunk.duration_seconds)

    def to_processed(self, chunk, local_seconds: float) -> float:
        """A time inside a chunk file, on the processed clock."""
        if self.mode == "identity":
            return float(chunk.start_ts) + local_seconds
        return self.chunk_offsets[chunk.chunk_index] + local_seconds

    def _piece_at(self, processed: float) -> Piece | None:
        if not self.pieces:
            return None
        index = bisect_right(self._starts, processed) - 1
        return self.pieces[max(0, min(index, len(self.pieces) - 1))]

    def to_lecture(self, processed: float) -> float:
        """Processed-clock time -> time in the original lecture."""
        if self.mode == "identity" or not self.pieces:
            return processed
        piece = self._piece_at(processed)
        if processed <= piece.optimized_start:
            return piece.original_start
        if processed >= piece.optimized_end:
            return piece.original_end
        return piece.original_start + (processed - piece.optimized_start) * piece.speed

    def speed_at(self, processed: float) -> float:
        """How much faster than natural the audio plays at this point (1.0 = natural)."""
        if self.mode == "identity":
            return 1.0
        piece = self._piece_at(processed)
        return piece.speed if piece else 1.0


def build_pieces(raw_mappings: Iterable[Any] | None) -> list[Piece]:
    pieces: list[Piece] = []
    for raw in raw_mappings or []:
        if not isinstance(raw, dict):
            continue
        values = [_number(raw.get(key)) for key in ("optimized_start", "optimized_end", "original_start", "original_end")]
        if any(value is None for value in values):
            continue
        optimized_start, optimized_end, original_start, original_end = values
        if optimized_end <= optimized_start or original_end < original_start:
            continue
        pieces.append(Piece(optimized_start, optimized_end, original_start, original_end))
    pieces.sort(key=lambda piece: piece.optimized_start)
    return pieces


def build_timeline(chunks: list, *, raw_mappings: Iterable[Any] | None, processed_duration: float | None, tolerance: float) -> tuple[Timeline | None, dict]:
    """Return (timeline, details). The timeline is None when the audio moment cannot be trusted."""
    details: dict[str, Any] = {"chunks": len(chunks), "tolerance_s": tolerance}
    if not chunks:
        return None, {**details, "problem": "no_chunks"}
    ordered = sorted(chunks, key=lambda chunk: chunk.chunk_index)
    pieces = build_pieces(raw_mappings)
    if not pieces:
        return None, {**details, "mode": "identity"}

    offsets: dict[int, float] = {}
    cursor = 0.0
    for chunk in ordered:
        offsets[chunk.chunk_index] = round(cursor, 3)
        cursor += float(chunk.duration_seconds)
    details.update({"mode": "mapped", "processed_end_s": round(cursor, 3), "mappings": len(pieces)})

    allowed = max(tolerance, 0.5)
    if processed_duration:
        difference = abs(cursor - float(processed_duration))
        details["duration_difference_s"] = round(difference, 3)
        if difference > allowed:
            return None, {**details, "problem": "duration_mismatch"}
    # The recorded mappings must cover the same processed audio the chunk files hold.
    covered = pieces[-1].optimized_end
    details["mappings_end_s"] = round(covered, 3)
    if abs(covered - cursor) > max(allowed, 1.0):
        return None, {**details, "problem": "mapping_mismatch"}
    for previous, current in zip(pieces, pieces[1:]):
        if current.optimized_start < previous.optimized_end - 0.05:
            return None, {**details, "problem": "mapping_overlap"}
    return Timeline(mode="mapped", chunk_offsets=offsets, pieces=pieces), details
