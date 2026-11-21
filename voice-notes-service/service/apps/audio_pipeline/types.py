from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Segment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class TimestampMapping:
    original_start: float
    original_end: float
    optimized_start: float
    optimized_end: float


@dataclass
class AudioChunkSpec:
    chunk_index: int
    optimized_start: float
    optimized_end: float
    original_start: float
    original_end: float

    @property
    def duration_seconds(self) -> float:
        return max(0.0, self.optimized_end - self.optimized_start)


@dataclass
class SegmentOptimizationResult:
    chunk_relative_path: str
    chunk_local_path: str
    optimized_duration: float
    speech_duration: float
    silence_removed_seconds: float
    speed_multiplier: float
    speech_ratio: float
    detected_speech_ratio: float
    used_safe_speedup_fallback: bool
    used_conservative_audio_fallback: bool
    speech_segments: list[Segment] = field(default_factory=list)
    silence_segments: list[Segment] = field(default_factory=list)
    local_mappings: list[TimestampMapping] = field(default_factory=list)
    temp_paths: list[str] = field(default_factory=list)


@dataclass
class AudioProcessingResult:
    original_duration: float
    optimized_duration: float
    silence_removed_seconds: float
    speed_multiplier: float
    chunk_count: int
    estimated_cost_reduction_percent: float
    speech_ratio: float
    normalized_audio_path: str
    optimized_audio_path: str
    processing_mode: str = "whole_file"
    temp_paths: list[str] = field(default_factory=list)
    chunk_specs: list[AudioChunkSpec] = field(default_factory=list)
    speech_segments: list[dict[str, float]] = field(default_factory=list)
    silence_segments: list[dict[str, float]] = field(default_factory=list)
    timestamp_mappings: list[dict[str, float]] = field(default_factory=list)
    waveform_preview_data: list[float] = field(default_factory=list)
    processing_steps: list[dict[str, object]] = field(default_factory=list)
