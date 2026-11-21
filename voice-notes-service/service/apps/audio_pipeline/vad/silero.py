from __future__ import annotations

import math
import struct
import wave
from contextlib import closing

from django.conf import settings

from apps.audio_pipeline.types import Segment

try:  # pragma: no cover - optional dependency
    from silero_vad import get_speech_timestamps, load_silero_vad, read_audio  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    get_speech_timestamps = None
    load_silero_vad = None
    read_audio = None


class SileroVADService:
    @staticmethod
    def _pcm_rms(*, data: bytes, sample_width: int, channels: int) -> float:
        if sample_width != 2 or not data:
            return 0.0
        sample_count = len(data) // sample_width
        if sample_count <= 0:
            return 0.0
        samples = struct.unpack("<" + "h" * sample_count, data[: sample_count * sample_width])
        if channels > 1:
            mono_samples = []
            for index in range(0, len(samples), channels):
                frame = samples[index : index + channels]
                mono_samples.append(sum(frame) / len(frame))
            samples = tuple(int(value) for value in mono_samples)
        power = sum(sample * sample for sample in samples) / max(1, len(samples))
        return math.sqrt(power)

    def detect(self, *, wav_path: str) -> tuple[list[Segment], list[Segment], float]:
        if load_silero_vad and get_speech_timestamps and read_audio:  # pragma: no branch
            model = load_silero_vad()
            audio = read_audio(wav_path, sampling_rate=16000)
            timestamps = get_speech_timestamps(audio, model, sampling_rate=16000)
            speech_segments = [
                Segment(start=round(item["start"] / 16000, 3), end=round(item["end"] / 16000, 3))
                for item in timestamps
            ]
            return self._finalize_segments(wav_path=wav_path, speech_segments=speech_segments)

        return self._fallback_energy_vad(wav_path=wav_path)

    def _read_energies(self, *, wav_path: str, frame_ms: int) -> tuple[list[float], int]:
        with closing(wave.open(wav_path, "rb")) as wav_file:
            sample_rate = wav_file.getframerate()
            sample_width = wav_file.getsampwidth()
            channels = wav_file.getnchannels()
            frame_size = max(1, int(sample_rate * frame_ms / 1000))
            energies: list[float] = []
            while True:
                data = wav_file.readframes(frame_size)
                if not data:
                    break
                rms = self._pcm_rms(data=data, sample_width=sample_width, channels=channels)
                energies.append(float(rms))
        return energies, sample_rate

    def _segments_from_energies(
        self,
        *,
        energies: list[float],
        frame_ms: int,
        threshold_multiplier: float,
        min_threshold: float,
        min_speech_ms: int,
        min_silence_ms: int,
    ) -> list[Segment]:
        if not energies:
            return []

        baseline = sum(energies) / len(energies)
        threshold = max(min_threshold, baseline * threshold_multiplier)
        speech_segments: list[Segment] = []
        current_start: float | None = None

        for index, energy in enumerate(energies):
            current_ts = index * (frame_ms / 1000)
            is_speech = energy >= threshold
            if is_speech and current_start is None:
                current_start = current_ts
            if not is_speech and current_start is not None:
                speech_segments.append(Segment(start=current_start, end=current_ts))
                current_start = None

        if current_start is not None:
            speech_segments.append(Segment(start=current_start, end=len(energies) * (frame_ms / 1000)))

        merged: list[Segment] = []
        min_speech_seconds = min_speech_ms / 1000
        min_silence_seconds = min_silence_ms / 1000
        for segment in speech_segments:
            if segment.duration < min_speech_seconds:
                continue
            if merged and segment.start - merged[-1].end <= min_silence_seconds:
                merged[-1].end = segment.end
            else:
                merged.append(segment)
        return merged

    def _fallback_energy_vad(self, *, wav_path: str) -> tuple[list[Segment], list[Segment], float]:
        frame_ms = 30
        energies, _sample_rate = self._read_energies(wav_path=wav_path, frame_ms=frame_ms)
        if not energies:
            return [], [], 0.0

        primary_segments = self._segments_from_energies(
            energies=energies,
            frame_ms=frame_ms,
            threshold_multiplier=1.15,
            min_threshold=220.0,
            min_speech_ms=240,
            min_silence_ms=360,
        )
        primary_result = self._finalize_segments(wav_path=wav_path, speech_segments=primary_segments)

        if primary_result[2] >= 0.35:
            return primary_result

        relaxed_segments = self._segments_from_energies(
            energies=energies,
            frame_ms=frame_ms,
            threshold_multiplier=0.9,
            min_threshold=140.0,
            min_speech_ms=150,
            min_silence_ms=600,
        )
        relaxed_result = self._finalize_segments(wav_path=wav_path, speech_segments=relaxed_segments)
        if relaxed_result[2] > primary_result[2] + 0.12:
            return relaxed_result
        return primary_result

    def _finalize_segments(self, *, wav_path: str, speech_segments: list[Segment]) -> tuple[list[Segment], list[Segment], float]:
        with closing(wave.open(wav_path, "rb")) as wav_file:
            total_duration = wav_file.getnframes() / float(wav_file.getframerate())

        padding_seconds = max(0.0, float(settings.AUDIO_VAD_PADDING_SECONDS))
        if total_duration > 180:
            padding_seconds = max(padding_seconds, 0.8)
        padded_segments: list[Segment] = []
        for segment in speech_segments:
            padded_segments.append(
                Segment(
                    start=round(max(0.0, segment.start - padding_seconds), 3),
                    end=round(min(total_duration, segment.end + padding_seconds), 3),
                )
            )

        merged_segments: list[Segment] = []
        for segment in padded_segments:
            if merged_segments and segment.start <= merged_segments[-1].end:
                merged_segments[-1].end = round(max(merged_segments[-1].end, segment.end), 3)
            else:
                merged_segments.append(segment)

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
