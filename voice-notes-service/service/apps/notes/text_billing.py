"""Minutes charged for turning pasted text into a note.

A voice upload is charged by its length, but a note made from text has no audio length, and in
emergency mode the AI is also several times more expensive. So the charge follows what the note
really costs us, and keeps the same profit margin every plan has per minute:

    minutes = ceil(estimated_cost_toman / (REAL_COST_PER_60_MINUTES_TOMAN / 60)), at least 5

The estimated cost comes from the tokens that real v4 notes of that text size used (measured on the
production notes of 2026-11-06) multiplied by the price of the model that will write the note.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from django.conf import settings

from apps.admin_ops.system_mode import is_emergency

# (transcript characters, input tokens, output tokens) of real v4 notes; the last two rows are
# extrapolated because a v4 note's length is capped, so longer texts cost only a little more.
_TOKEN_TABLE: tuple[tuple[int, int, int], ...] = (
    (0, 12_000, 3_000),
    (2_650, 27_400, 7_500),
    (4_755, 56_500, 13_900),
    (9_800, 91_000, 24_000),
    (20_800, 153_000, 24_700),
    (28_000, 142_000, 29_500),
    (45_000, 190_000, 32_000),
    (60_000, 220_000, 34_000),
)

# Toman per million tokens (input, output). luna is the real price our AvalAI ledger shows; the
# others are the providers' listed prices (GapGPT's dollar price at the ledger's implied rate).
_PRICES: dict[str, tuple[int, int]] = {
    "gpt-6-luna": (23_200, 116_000),
    "gpt-5.6-luna": (23_200, 116_000),
    "gapgpt-deepseek-v4.1-flash": (34_800, 139_200),
    "gemini-2.5-flash-lite": (7_000, 28_000),
    "Qwen3.5-35B-A3B": (59_150, 682_500),
    "Xerxes-1": (52_000, 210_000),
}
_UNKNOWN_MODEL_PRICE = _PRICES["Qwen3.5-35B-A3B"]  # an unlisted model is priced like the dearest one


@dataclass(frozen=True)
class TextNoteQuote:
    chars: int
    model: str
    estimated_cost_toman: int
    minutes: int


def _tokens_for(chars: int) -> tuple[float, float]:
    chars = max(0, int(chars))
    for (c0, i0, o0), (c1, i1, o1) in zip(_TOKEN_TABLE, _TOKEN_TABLE[1:]):
        if chars <= c1:
            fraction = (chars - c0) / (c1 - c0)
            return i0 + fraction * (i1 - i0), o0 + fraction * (o1 - o0)
    last = _TOKEN_TABLE[-1]
    return float(last[1]), float(last[2])


def writing_model() -> str:
    """The model that will write the note right now (the emergency primary while emergency is on)."""
    if is_emergency():
        return str(settings.EMERGENCY_AI_PRIMARY_MODEL)
    return str(settings.NOTE_PRIMARY_MODEL)


def quote_text_note(*, chars: int, model: str | None = None) -> TextNoteQuote:
    model = model or writing_model()
    price_in, price_out = _PRICES.get(model, _UNKNOWN_MODEL_PRICE)
    tokens_in, tokens_out = _tokens_for(chars)
    cost = (tokens_in * price_in + tokens_out * price_out) / 1_000_000
    per_minute = max(1.0, settings.REAL_COST_PER_60_MINUTES_TOMAN / 60)
    minutes = max(int(settings.TEXT_NOTE_MIN_MINUTES), math.ceil(cost / per_minute))
    return TextNoteQuote(chars=int(chars), model=model, estimated_cost_toman=int(round(cost)), minutes=minutes)
