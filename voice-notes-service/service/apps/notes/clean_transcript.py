"""The "whole class" note: the transcript, cleaned but not rewritten.

The model only corrects words the recogniser misheard (from context), adds punctuation, splits the
text into paragraphs and names a section when the topic changes. It must not summarise, reorder,
explain or add. Every batch is checked against its source: when the cleaned text drifts too far
from what was said (too short, too long, or too many changed words) the batch falls back to the
recogniser's own text, so nothing the lecturer said is lost or invented.

The result is a normal semantic document (section headers and paragraphs with the class time)
marked ``document_kind: "transcript"``, which the renderer draws as a plain reading page.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from difflib import SequenceMatcher
from typing import Any, Callable

from django.db import connections

from apps.ai_pipeline.exceptions import PipelineError

logger = logging.getLogger("tesanote")

BATCH_CHARS = 3200
MAX_PARALLEL = 4
MIN_SIMILARITY = 0.72
MIN_LENGTH_RATIO = 0.85
MAX_LENGTH_RATIO = 1.18
PARAGRAPHS_PER_PAGE = 9

SYSTEM_PROMPT = (
    "You clean the transcript of a university class that a speech recogniser wrote. Output JSON only.\n"
    "Your ONLY jobs:\n"
    "1. Correct words the recogniser clearly misheard, using the surrounding context and the subject "
    "of the class (a nonsense word that sounds like a real term becomes that term).\n"
    "2. Add punctuation and split the text into readable paragraphs (3-7 sentences each).\n"
    "3. When the lecturer clearly moves to a new topic, give that paragraph a short heading made of the "
    "lecturer's own words (max 8 words). Most paragraphs have no heading.\n"
    "Never summarise, shorten, reorder, explain, translate or add anything. Keep every sentence, "
    "example, joke, repetition and remark of the lecturer, in the same order and the same spoken "
    "register (keep colloquial forms such as «میشه» «اینو»). Remove only recogniser noise: a word "
    "repeated by mistake, or a line that is clearly not speech. Words in another language (English, "
    "Arabic, ...) stay in that language and script, written correctly. Do not add a title for the class.\n"
    'Return {"paragraphs": [{"heading": "", "text": "..."}]} covering ALL of the input text.'
)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[\w؀-ۿ]+", str(text or "").replace("‌", " ").casefold())


def fidelity(source: str, cleaned: str) -> dict[str, float]:
    """How close the cleaned text stays to the source (word level)."""
    a, b = _tokens(source), _tokens(cleaned)
    if not a:
        return {"similarity": 1.0, "length_ratio": 1.0}
    return {
        "similarity": round(SequenceMatcher(None, a, b, autojunk=False).ratio(), 4),
        "length_ratio": round(len(b) / len(a), 4),
    }


def faithful(source: str, cleaned: str) -> bool:
    score = fidelity(source, cleaned)
    return score["similarity"] >= MIN_SIMILARITY and MIN_LENGTH_RATIO <= score["length_ratio"] <= MAX_LENGTH_RATIO


def make_batches(chunks: list[dict[str, Any]], *, limit: int = BATCH_CHARS) -> list[dict[str, Any]]:
    """Consecutive transcript chunks joined into batches of about ``limit`` characters."""
    batches: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for chunk in chunks:
        text = str(chunk.get("text", "") or "").strip()
        if not text:
            continue
        start = float(chunk.get("start_ts", 0) or 0)
        end = float(chunk.get("end_ts", start) or start)
        if current is None or len(current["text"]) + len(text) > limit:
            current = {"text": text, "start": start, "end": end}
            batches.append(current)
        else:
            current["text"] = f"{current['text']} {text}"
            current["end"] = max(current["end"], end)
    return batches


def _split_raw(text: str) -> list[dict[str, str]]:
    """Fallback paragraphs: the recogniser's text cut every ~5 sentences (or ~600 characters)."""
    sentences = [part.strip() for part in re.split(r"(?<=[.!?؟])\s+", text) if part.strip()]
    if len(sentences) <= 1:
        words = text.split()
        sentences = [" ".join(words[i : i + 90]) for i in range(0, len(words), 90)] or [text]
    paragraphs: list[dict[str, str]] = []
    buffer: list[str] = []
    for sentence in sentences:
        buffer.append(sentence)
        if len(buffer) >= 5 or sum(len(item) for item in buffer) > 600:
            paragraphs.append({"heading": "", "text": " ".join(buffer)})
            buffer = []
    if buffer:
        paragraphs.append({"heading": "", "text": " ".join(buffer)})
    return paragraphs


def clock(seconds: float, *, persian: bool = True) -> str:
    total = max(0, int(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
    return text.translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")) if persian else text


def clean_batch(provider: Any, batch: dict[str, Any], *, course: str, title: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """One model call for one batch; returns (paragraphs, report). Falls back to the raw text."""
    user_prompt = (
        f"Course: {course or '-'}\nSession topic: {title or '-'}\n\n"
        f"Transcript part (keep all of it):\n{batch['text']}"
    )
    usage: dict[str, int] = {}
    try:
        payload, result = provider.complete_json(system_prompt=SYSTEM_PROMPT, user_prompt=user_prompt)
        usage = dict(result.usage or {})
        paragraphs = [
            {"heading": str(item.get("heading", "") or "").strip()[:120], "text": str(item.get("text", "") or "").strip()}
            for item in (payload.get("paragraphs") or [])
            if isinstance(item, dict) and str(item.get("text", "") or "").strip()
        ]
    except PipelineError as exc:
        logger.warning("clean_transcript_batch_failed", extra={"error": exc.message})
        return _split_raw(batch["text"]), {"fallback": "model_error", "usage": usage}
    cleaned = " ".join(item["text"] for item in paragraphs)
    score = fidelity(batch["text"], cleaned)
    if not paragraphs or not faithful(batch["text"], cleaned):
        return _split_raw(batch["text"]), {"fallback": "drifted", **score, "usage": usage}
    return paragraphs, {"fallback": "", **score, "usage": usage}


def build_document(
    *,
    batches: list[dict[str, Any]],
    results: list[list[dict[str, str]]],
    to_lecture_time: Callable[[float], float],
    header: dict[str, Any],
    title: str,
    persian: bool = True,
) -> dict[str, Any]:
    """Semantic pages: a section header where the topic changes, each paragraph with its class time."""
    blocks: list[dict[str, Any]] = []
    for batch, paragraphs in zip(batches, results):
        total = sum(len(item["text"]) for item in paragraphs) or 1
        seen = 0
        for item in paragraphs:
            # A paragraph's time: its share of the batch, placed on the batch's own span.
            moment = batch["start"] + (batch["end"] - batch["start"]) * (seen / total)
            seen += len(item["text"])
            if item["heading"]:
                blocks.append({"type": "section-header", "title": item["heading"], "accent_dot": False})
            blocks.append(
                {
                    "type": "paragraph",
                    "variants": ["transcript"],
                    "content": item["text"],
                    "time_label": clock(to_lecture_time(moment), persian=persian),
                }
            )
    pages: list[dict[str, Any]] = []
    page: list[dict[str, Any]] = []
    paragraphs_on_page = 0
    for block in blocks:
        if block["type"] == "paragraph":
            if paragraphs_on_page >= PARAGRAPHS_PER_PAGE:
                pages.append({"header": dict(header), "blocks": page})
                page, paragraphs_on_page = [], 0
            paragraphs_on_page += 1
        page.append(block)
    if page:
        pages.append({"header": dict(header), "blocks": page})
    return {"document_title": title, "document_kind": "transcript", "pages": pages}


def clean_transcript_document(
    *,
    provider: Any,
    chunks: list[dict[str, Any]],
    to_lecture_time: Callable[[float], float],
    header: dict[str, Any],
    course: str,
    title: str,
    persian: bool = True,
) -> tuple[dict[str, Any], dict[str, int], dict[str, Any]]:
    """Clean every batch (a few in parallel) and build the document. Returns (document, usage, report)."""
    batches = make_batches(chunks)
    if not batches:
        raise PipelineError(stage="NOTE_GENERATION", message="Transcript is empty; the class text cannot be built.")

    def work(batch: dict[str, Any]):
        try:
            return clean_batch(provider, batch, course=course, title=title)
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(batches)), thread_name_prefix="clean-transcript") as pool:
        futures = [pool.submit(copy_context().run, work, batch) for batch in batches]
        outcomes = [future.result() for future in futures]
    results = [paragraphs for paragraphs, _ in outcomes]
    reports = [report for _, report in outcomes]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for report in reports:
        for key in usage:
            usage[key] += int((report.get("usage") or {}).get(key, 0) or 0)
    document = build_document(
        batches=batches, results=results, to_lecture_time=to_lecture_time, header=header, title=title, persian=persian
    )
    report = {
        "batches": len(batches),
        "fallback_batches": sum(1 for item in reports if item.get("fallback")),
        "fallback_reasons": [item.get("fallback") for item in reports if item.get("fallback")],
        "similarity": [item.get("similarity") for item in reports if "similarity" in item],
    }
    return document, usage, report
