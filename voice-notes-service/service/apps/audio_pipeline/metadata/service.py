from __future__ import annotations

import math

from django.conf import settings


class AudioMetadataService:
    @staticmethod
    def billable_units_for_duration(*, duration_seconds: float, billing_unit_seconds: float) -> int:
        if duration_seconds <= 0 or billing_unit_seconds <= 0:
            return 0
        return int(math.ceil(duration_seconds / billing_unit_seconds))

    def billable_units_for_chunks(self, *, chunk_durations: list[float], billing_unit_seconds: float) -> int:
        return sum(
            self.billable_units_for_duration(
                duration_seconds=max(0.0, float(duration)),
                billing_unit_seconds=billing_unit_seconds,
            )
            for duration in chunk_durations
        )

    @staticmethod
    def estimated_cost_reduction_percent(*, original_duration: float, optimized_duration: float) -> float:
        if original_duration <= 0:
            return 0.0
        return round(max(0.0, (1 - (optimized_duration / original_duration)) * 100), 2)

    @staticmethod
    def estimated_transcription_cost(*, processed_duration: float, request_count: int) -> dict[str, float]:
        variable_cost = round((max(0.0, processed_duration) / 60.0) * settings.GAPGPT_WHISPER_PRICE_PER_MINUTE, 6)
        job_floor_cost = settings.GAPGPT_WHISPER_MIN_JOB_COST if request_count > 0 else 0.0
        billed_cost = round(max(variable_cost, job_floor_cost), 6)
        return {
            "variable_cost": variable_cost,
            "job_floor_cost": round(job_floor_cost, 6),
            "billed_cost": billed_cost,
        }
