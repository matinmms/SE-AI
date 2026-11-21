from __future__ import annotations

from django.db import models

from apps.common.models import BaseModel


class AudioChunk(BaseModel):
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="audio_chunks")
    chunk_index = models.PositiveIntegerField()
    start_ts = models.FloatField()
    end_ts = models.FloatField()
    duration_seconds = models.FloatField()
    original_audio_path = models.CharField(max_length=500)
    optimized_audio_path = models.CharField(max_length=500)

    class Meta:
        ordering = ("chunk_index",)
        constraints = [
            models.UniqueConstraint(fields=["lecture", "chunk_index"], name="unique_audio_chunk_per_lecture"),
        ]
        indexes = [
            models.Index(fields=["lecture", "chunk_index"]),
        ]

    def __str__(self) -> str:
        return f"{self.lecture_id}:{self.chunk_index}"


class TranscriptChunk(BaseModel):
    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="transcript_chunks")
    audio_chunk = models.OneToOneField("audio_pipeline.AudioChunk", on_delete=models.CASCADE, related_name="transcript_chunk")
    chunk_index = models.PositiveIntegerField()
    text = models.TextField(blank=True)
    raw_text = models.TextField(blank=True)
    start_ts = models.FloatField()
    end_ts = models.FloatField()
    language = models.CharField(max_length=32, blank=True)
    api_cost = models.FloatField(default=0)
    processing_time_ms = models.PositiveIntegerField(default=0)
    confidence_score = models.FloatField(null=True, blank=True)
    provider_job_id = models.CharField(max_length=255, blank=True)
    provider_trace_id = models.CharField(max_length=255, blank=True)
    hotwords_used = models.JSONField(default=list, blank=True)
    transcript_source = models.CharField(max_length=64, blank=True)
    processing_metadata = models.JSONField(default=dict, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ("chunk_index",)
        constraints = [
            models.UniqueConstraint(fields=["lecture", "chunk_index"], name="unique_transcript_chunk_per_lecture"),
        ]
        indexes = [
            models.Index(fields=["lecture", "chunk_index"]),
        ]

    def __str__(self) -> str:
        return f"{self.lecture_id}:transcript:{self.chunk_index}"


class TranscriptSegment(BaseModel):
    """One speech-to-text segment with its recognizer statistics, on the lecture's absolute timeline.

    Captured only when the accuracy check is enabled for the lecture's owner. `start_abs` and
    `end_abs` are computed once at write time (audio chunk start + part offset + segment time)
    so nothing later has to do clock arithmetic. The Whisper statistics are per decode window,
    so segments of one window share them; `window_index` groups them.
    """

    lecture = models.ForeignKey("lectures.Lecture", on_delete=models.CASCADE, related_name="transcript_segments")
    audio_chunk = models.ForeignKey("audio_pipeline.AudioChunk", on_delete=models.CASCADE, related_name="segments")
    segment_index = models.PositiveIntegerField()
    window_index = models.PositiveIntegerField(default=0)
    start_abs = models.FloatField()
    end_abs = models.FloatField()
    text = models.TextField(blank=True)
    avg_logprob = models.FloatField(null=True, blank=True)
    no_speech_prob = models.FloatField(null=True, blank=True)
    compression_ratio = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ("start_abs", "segment_index")
        constraints = [
            models.UniqueConstraint(fields=["audio_chunk", "segment_index"], name="unique_segment_per_audio_chunk"),
        ]
        indexes = [
            models.Index(fields=["lecture", "start_abs"], name="audio_seg_lecture_start_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.lecture_id}:seg:{self.start_abs:.1f}"
