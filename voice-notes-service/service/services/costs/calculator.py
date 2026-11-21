from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CostBreakdown:
    audio_minutes: int
    avenger_cost: int
    llm_cost: int
    pdf_cost: int
    storage_cost: int
    total_cost: int


class CostCalculatorService:
    COST_PER_AUDIO_MINUTE_TOMAN = 300

    @classmethod
    def estimate_for_audio_minutes(cls, *, audio_minutes: int) -> CostBreakdown:
        avenger_cost = max(0, audio_minutes) * cls.COST_PER_AUDIO_MINUTE_TOMAN
        llm_cost = 0
        pdf_cost = 0
        storage_cost = 0
        total_cost = avenger_cost + llm_cost + pdf_cost + storage_cost
        return CostBreakdown(
            audio_minutes=max(0, audio_minutes),
            avenger_cost=avenger_cost,
            llm_cost=llm_cost,
            pdf_cost=pdf_cost,
            storage_cost=storage_cost,
            total_cost=total_cost,
        )

