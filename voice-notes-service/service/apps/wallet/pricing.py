"""What things cost, in toman, and what the user is charged.

A note is charged:  real cost of the requests made for it  +  a profit that depends only on the
audio minutes (`WALLET_PROFIT_TOMAN_PER_MINUTE`). The real cost comes from the AI cost ledger: the
provider-reported amount once a request is reconciled, otherwise tokens times the model's price.
Speech-to-text has no tokens, so it is charged per audio minute (`WALLET_STT_TOMAN_PER_MINUTE`).

Add-ons (summary, flashcards/quiz, tutor chat) are charged per request at their real cost.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from django.conf import settings

# Features that are charged request by request, outside the note itself.
ADDON_FEATURES = frozenset({"summary", "study_items", "chatbot"})
# Features that belong to the note itself and are summed into its charge (transcription is per minute).
NOTE_FEATURES_EXCLUDED = ADDON_FEATURES | {"transcription"}


def round_up(value: float | Decimal, step: int = 100) -> int:
    return int(math.ceil(float(value) / step) * step)


def round_down(value: float | Decimal, step: int = 100) -> int:
    return int(math.floor(float(value) / step) * step)


def minutes_for_seconds(seconds: float | int) -> int:
    return max(1, math.ceil(max(0.0, float(seconds)) / 60))


def per_minute_estimate_toman() -> float:
    """Typical price of one audio minute: average real cost plus the per-minute profit."""
    return settings.REAL_COST_PER_60_MINUTES_TOMAN / 60 + settings.WALLET_PROFIT_TOMAN_PER_MINUTE


def estimate_for_minutes(minutes: int) -> int:
    """The approximate price shown before processing (rounded up to 500 toman)."""
    if minutes <= 0:
        return 0
    return max(int(settings.WALLET_MIN_NOTE_CHARGE_TOMAN), round_up(minutes * per_minute_estimate_toman(), 500))


def hold_for_estimate(estimate: int) -> int:
    return round_up(estimate * settings.WALLET_HOLD_FACTOR_PERCENT / 100, 500)


def token_cost_toman(model: str, input_tokens: int | None, output_tokens: int | None) -> float:
    from apps.notes.text_billing import _PRICES, _UNKNOWN_MODEL_PRICE

    price_in, price_out = _PRICES.get(model, _UNKNOWN_MODEL_PRICE)
    return (int(input_tokens or 0) * price_in + int(output_tokens or 0) * price_out) / 1_000_000


def request_cost_toman(row: Any) -> float:
    """Real cost of one ledger row: the provider's own number when reconciled, else tokens x price."""
    exact = getattr(row, "effective_irt_consumed", None)
    if exact is not None:
        return float(exact)
    return token_cost_toman(
        str(getattr(row, "requested_model", "") or ""),
        getattr(row, "input_tokens", None),
        getattr(row, "output_tokens", None),
    )


def addon_charge_toman(cost_toman: float) -> int:
    """Charge for one add-on request: its real cost plus the configured markup, at least 1 toman."""
    charged = cost_toman * (100 + settings.WALLET_ADDON_MARKUP_PERCENT) / 100
    return max(1, int(math.ceil(charged)))


def note_cost_breakdown(lecture: Any, *, minutes: int) -> dict[str, int]:
    """Everything a finished note costs, and what it is charged, in toman."""
    from apps.ai_costs.models import AIProviderRequest

    text_only = (getattr(lecture, "processing_metadata", None) or {}).get("source_mode") == "text_only"
    stt = 0 if text_only else int(math.ceil(minutes * settings.WALLET_STT_TOMAN_PER_MINUTE))  # pasted text has no audio
    rows = AIProviderRequest.objects.filter(lecture_id=lecture.id).exclude(feature__in=NOTE_FEATURES_EXCLUDED)
    llm = 0.0
    counted = 0
    for row in rows:
        llm += request_cost_toman(row)
        counted += 1
    if counted == 0:
        # No ledger rows (tracking off, or an old lecture): fall back to the measured average.
        llm = max(0.0, minutes * settings.REAL_COST_PER_60_MINUTES_TOMAN / 60 - stt)
    profit = int(minutes * settings.WALLET_PROFIT_TOMAN_PER_MINUTE)
    total = max(int(settings.WALLET_MIN_NOTE_CHARGE_TOMAN), round_up(stt + llm + profit, 100))
    return {
        "minutes": minutes,
        "stt_toman": stt,
        "llm_toman": int(math.ceil(llm)),
        "profit_toman": profit,
        "requests_counted": counted,
        "total_toman": total,
    }
