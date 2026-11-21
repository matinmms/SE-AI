from __future__ import annotations

import struct
import wave
from contextlib import closing


class WaveformPreviewService:
    @staticmethod
    def _peak(*, data: bytes, sample_width: int, channels: int) -> float:
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
        return float(max(abs(sample) for sample in samples))

    def generate(self, *, wav_path: str, points: int = 72) -> list[float]:
        with closing(wave.open(wav_path, "rb")) as wav_file:
            total_frames = wav_file.getnframes()
            sample_width = wav_file.getsampwidth()
            channels = wav_file.getnchannels()
            if total_frames <= 0:
                return []

            frames_per_bucket = max(1, total_frames // points)
            peaks: list[float] = []
            for _ in range(points):
                frames = wav_file.readframes(frames_per_bucket)
                if not frames:
                    break
                peak = self._peak(data=frames, sample_width=sample_width, channels=channels)
                peaks.append(round(min(1.0, peak / 32767), 4))

        return peaks
