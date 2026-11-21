from __future__ import annotations

from apps.audio_pipeline.models import TranscriptChunk


class TranscriptMergingService:
    def merge(self, *, chunks: list[TranscriptChunk]) -> tuple[str, list[dict[str, object]], str]:
        ordered_chunks = sorted(chunks, key=lambda chunk: chunk.chunk_index)
        full_transcript = "\n\n".join(chunk.text.strip() for chunk in ordered_chunks if chunk.text.strip())
        timestamps = [
            {
                "chunk_index": chunk.chunk_index,
                "start_ts": chunk.start_ts,
                "end_ts": chunk.end_ts,
                "text": chunk.text,
            }
            for chunk in ordered_chunks
        ]
        language = next((chunk.language for chunk in ordered_chunks if chunk.language), "")
        return full_transcript.strip(), timestamps, language
