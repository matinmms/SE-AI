from __future__ import annotations

import math

from apps.audio_pipeline.types import AudioChunkSpec, TimestampMapping


class AudioChunkingService:
    @staticmethod
    def _split_mapping(*, mapping: TimestampMapping, max_seconds: float) -> list[TimestampMapping]:
        mapping_duration = max(0.0, mapping.optimized_end - mapping.optimized_start)
        if mapping_duration <= max_seconds:
            return [mapping]

        parts: list[TimestampMapping] = []
        optimized_cursor = mapping.optimized_start
        original_cursor = mapping.original_start
        original_duration = max(0.0, mapping.original_end - mapping.original_start)
        ratio = original_duration / mapping_duration if mapping_duration else 1.0

        while optimized_cursor < mapping.optimized_end:
            optimized_end = min(mapping.optimized_end, optimized_cursor + max_seconds)
            optimized_slice_duration = optimized_end - optimized_cursor
            original_slice_duration = optimized_slice_duration * ratio
            original_end = min(mapping.original_end, original_cursor + original_slice_duration)
            parts.append(
                TimestampMapping(
                    original_start=round(original_cursor, 3),
                    original_end=round(original_end, 3),
                    optimized_start=round(optimized_cursor, 3),
                    optimized_end=round(optimized_end, 3),
                )
            )
            optimized_cursor = optimized_end
            original_cursor = original_end

        return parts

    def build_chunks(
        self,
        *,
        mappings: list[TimestampMapping],
        target_seconds: float = 24,
        max_seconds: float = 29,
        min_seconds: float = 4,
        billing_unit_seconds: float | None = None,
    ) -> list[AudioChunkSpec]:
        if not mappings:
            return []

        flattened_mappings: list[TimestampMapping] = []
        for mapping in mappings:
            flattened_mappings.extend(self._split_mapping(mapping=mapping, max_seconds=max_seconds))

        chunks: list[AudioChunkSpec] = []
        current_group: list[TimestampMapping] = []
        current_duration = 0.0

        def flush() -> None:
            nonlocal current_group, current_duration
            if not current_group:
                return
            first = current_group[0]
            last = current_group[-1]
            chunks.append(
                AudioChunkSpec(
                    chunk_index=len(chunks),
                    optimized_start=round(first.optimized_start, 3),
                    optimized_end=round(last.optimized_end, 3),
                    original_start=round(first.original_start, 3),
                    original_end=round(last.original_end, 3),
                )
            )
            current_group = []
            current_duration = 0.0

        for mapping in flattened_mappings:
            mapping_duration = max(0.0, mapping.optimized_end - mapping.optimized_start)
            if current_group and current_duration + mapping_duration > max_seconds:
                flush()
            current_group.append(mapping)
            current_duration += mapping_duration
            if current_duration >= target_seconds:
                flush()

        flush()
        if len(chunks) >= 2 and chunks[-1].duration_seconds < min_seconds:
            previous = chunks[-2]
            trailing = chunks[-1]
            combined_duration = previous.duration_seconds + trailing.duration_seconds
            if combined_duration <= max_seconds:
                chunks[-2] = AudioChunkSpec(
                    chunk_index=previous.chunk_index,
                    optimized_start=previous.optimized_start,
                    optimized_end=trailing.optimized_end,
                    original_start=previous.original_start,
                    original_end=trailing.original_end,
                )
                chunks.pop()
                for index, chunk in enumerate(chunks):
                    chunk.chunk_index = index
        return self._merge_adjacent_chunks(
            chunks=chunks,
            max_seconds=max_seconds,
            billing_unit_seconds=billing_unit_seconds,
        )

    @staticmethod
    def _merge_adjacent_chunks(
        *,
        chunks: list[AudioChunkSpec],
        max_seconds: float,
        billing_unit_seconds: float | None,
    ) -> list[AudioChunkSpec]:
        if len(chunks) < 2:
            return chunks

        merged: list[AudioChunkSpec] = []
        current = chunks[0]
        for next_chunk in chunks[1:]:
            combined_duration = current.duration_seconds + next_chunk.duration_seconds
            if combined_duration <= max_seconds and AudioChunkingService._merge_is_billing_friendly(
                current_duration=current.duration_seconds,
                next_duration=next_chunk.duration_seconds,
                combined_duration=combined_duration,
                billing_unit_seconds=billing_unit_seconds,
            ):
                current = AudioChunkSpec(
                    chunk_index=current.chunk_index,
                    optimized_start=current.optimized_start,
                    optimized_end=next_chunk.optimized_end,
                    original_start=current.original_start,
                    original_end=next_chunk.original_end,
                )
                continue

            merged.append(current)
            current = next_chunk

        merged.append(current)
        for index, chunk in enumerate(merged):
            chunk.chunk_index = index
        return merged

    @staticmethod
    def _merge_is_billing_friendly(
        *,
        current_duration: float,
        next_duration: float,
        combined_duration: float,
        billing_unit_seconds: float | None,
    ) -> bool:
        if not billing_unit_seconds or billing_unit_seconds <= 0:
            return True
        separate_units = math.ceil(current_duration / billing_unit_seconds) + math.ceil(next_duration / billing_unit_seconds)
        combined_units = math.ceil(combined_duration / billing_unit_seconds)
        return combined_units <= separate_units
