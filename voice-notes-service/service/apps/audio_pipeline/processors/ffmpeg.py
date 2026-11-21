from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from apps.ai_pipeline.exceptions import PipelineError
from apps.audio_pipeline.types import Segment


class FFmpegProcessor:
    def __init__(self, *, ffmpeg_binary: str = "ffmpeg", ffprobe_binary: str = "ffprobe") -> None:
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary

    def ensure_available(self) -> None:
        if shutil.which(self.ffmpeg_binary) is None or shutil.which(self.ffprobe_binary) is None:
            raise PipelineError(stage="AUDIO_TOOLING", message="ffmpeg/ffprobe are not installed or not available in PATH.")

    def run(self, *, stage: str, args: list[str], threads: int | None = None) -> None:
        self.ensure_available()
        thread_args: list[str] = []
        if threads is not None and threads > 0:
            thread_args = ["-threads", str(threads)]
        result = subprocess.run(
            [self.ffmpeg_binary, "-y", *thread_args, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip().splitlines()
            summary = stderr[-1] if stderr else "Unknown ffmpeg failure."
            raise PipelineError(stage=stage, message=summary)

    def normalize(
        self,
        *,
        input_path: str,
        output_path: str,
        audio_filter: str | None = None,
        threads: int | None = None,
    ) -> None:
        self.run(
            stage="NORMALIZING",
            args=[
                "-i",
                input_path,
                "-ac",
                "1",
                "-ar",
                "16000",
                "-sample_fmt",
                "s16",
                "-af",
                audio_filter or "loudnorm=I=-16:LRA=11:TP=-1.5,highpass=f=80,lowpass=f=7600,afftdn=nf=-25",
                output_path,
            ],
            threads=threads,
        )

    def remove_silence_and_speedup(self, *, input_path: str, output_path: str, speed_multiplier: float) -> None:
        self.run(
            stage="SPEED_OPTIMIZING",
            args=[
                "-i",
                input_path,
                "-af",
                f"silenceremove=start_periods=1:start_duration=0.25:start_threshold=-50dB:stop_periods=-1:stop_duration=0.9:stop_threshold=-50dB:detection=rms,atempo={speed_multiplier}",
                output_path,
            ],
        )

    def speedup_only(self, *, input_path: str, output_path: str, speed_multiplier: float) -> None:
        self.run(
            stage="SPEED_OPTIMIZING",
            args=[
                "-i",
                input_path,
                "-af",
                f"atempo={speed_multiplier}",
                output_path,
            ],
        )

    def transcode_segment_to_wav(
        self,
        *,
        input_path: str,
        output_path: str,
        start_ts: float,
        duration_seconds: float,
        audio_filter: str | None = None,
        threads: int | None = None,
    ) -> None:
        duration = max(0.1, duration_seconds)
        args = [
            "-ss",
            f"{start_ts:.3f}",
            "-i",
            input_path,
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
        ]
        if audio_filter:
            args.extend(["-af", audio_filter])
        args.append(output_path)
        self.run(stage="NORMALIZING", args=args, threads=threads)

    def build_speech_only_audio(
        self,
        *,
        input_path: str,
        output_path: str,
        speech_segments: list[Segment],
        speed_multiplier: float,
        threads: int | None = None,
    ) -> None:
        if not speech_segments:
            self.speedup_only(input_path=input_path, output_path=output_path, speed_multiplier=speed_multiplier)
            return

        filter_parts: list[str] = []
        concat_inputs: list[str] = []
        for index, segment in enumerate(speech_segments):
            filter_parts.append(
                f"[0:a]atrim=start={segment.start:.3f}:end={segment.end:.3f},asetpts=PTS-STARTPTS[s{index}]"
            )
            concat_inputs.append(f"[s{index}]")

        filter_parts.append(
            f"{''.join(concat_inputs)}concat=n={len(speech_segments)}:v=0:a=1[merged]"
        )
        filter_parts.append(
            f"[merged]atempo={speed_multiplier},aformat=sample_fmts=s16:sample_rates=16000:channel_layouts=mono[outa]"
        )

        self.run(
            stage="SPEED_OPTIMIZING",
            args=[
                "-i",
                input_path,
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[outa]",
                output_path,
            ],
            threads=threads,
        )

    def transcode_segment_for_transcription(
        self,
        *,
        input_path: str,
        output_path: str,
        start_ts: float,
        duration_seconds: float,
        audio_filter: str | None = None,
        threads: int | None = None,
    ) -> None:
        duration = max(0.1, duration_seconds)
        args = [
            "-ss",
            f"{start_ts:.3f}",
            "-i",
            input_path,
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
        ]
        if audio_filter:
            args.extend(["-af", audio_filter])
        args.extend(
            [
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                output_path,
            ]
        )
        self.run(stage="CHUNKING", args=args, threads=threads)

    def transcode_segment_to_transcription_mp3(
        self,
        *,
        input_path: str,
        output_path: str,
        start_ts: float,
        duration_seconds: float,
        bitrate_kbps: int = 64,
        audio_filter: str | None = None,
        threads: int | None = None,
    ) -> None:
        duration = max(0.1, duration_seconds)
        args = [
            "-ss",
            f"{start_ts:.3f}",
            "-i",
            input_path,
            "-t",
            f"{duration:.3f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            "-codec:a",
            "libmp3lame",
            "-b:a",
            f"{max(16, bitrate_kbps)}k",
        ]
        if audio_filter:
            args.extend(["-af", audio_filter])
        args.append(output_path)
        self.run(stage="CHUNKING", args=args, threads=threads)

    def concat_audio_files(self, *, input_paths: list[str], output_path: str) -> None:
        if not input_paths:
            raise PipelineError(stage="CHUNKING", message="No audio inputs were provided for chunk concatenation.")
        if len(input_paths) == 1:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(input_paths[0], output_path)
            return

        self.ensure_available()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as manifest:
            manifest_path = manifest.name
            for input_path in input_paths:
                manifest.write(f"file {shlex.quote(input_path)}\n")
        try:
            self.run(
                stage="CHUNKING",
                args=[
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    manifest_path,
                    "-c",
                    "copy",
                    output_path,
                ],
            )
        finally:
            try:
                Path(manifest_path).unlink(missing_ok=True)
            except OSError:
                pass

    def extract_chunk(self, *, input_path: str, output_path: str, start_ts: float, end_ts: float) -> None:
        duration = max(0.1, end_ts - start_ts)
        output_is_mp3 = output_path.lower().endswith(".mp3")
        codec_args = (
            [
                "-codec:a",
                "libmp3lame",
                "-q:a",
                "4",
                "-ac",
                "1",
                "-ar",
                "16000",
            ]
            if output_is_mp3
            else [
                "-acodec",
                "pcm_s16le",
                "-ac",
                "1",
                "-ar",
                "16000",
            ]
        )
        self.run(
            stage="CHUNKING",
            args=[
                "-ss",
                f"{start_ts:.3f}",
                "-i",
                input_path,
                "-t",
                f"{duration:.3f}",
                *codec_args,
                output_path,
            ],
        )

    def probe_duration(self, *, input_path: str) -> float:
        self.ensure_available()
        result = subprocess.run(
            [
                self.ffprobe_binary,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                input_path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise PipelineError(stage="METADATA", message="Unable to probe audio duration.")
        payload = json.loads(result.stdout or "{}")
        return float(payload.get("format", {}).get("duration", 0.0) or 0.0)

    def ensure_parent(self, *, file_path: str) -> None:
        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
