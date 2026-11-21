from __future__ import annotations

import re


class TranscriptCleanupService:
    def clean(self, *, text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text).strip()
        cleaned = cleaned.replace("ي", "ی").replace("ك", "ک")
        cleaned = re.sub(r"\b(\w+)( \1\b)+", r"\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"(\b[\w\u0600-\u06FF]+)(\s+\1\b)+", r"\1", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+([،,:;.!?؟])", r"\1", cleaned)
        cleaned = re.sub(r"([.!?؟])(?=[^\s])", r"\1 ", cleaned)
        cleaned = re.sub(r"(،)(?=[^\s])", r"\1 ", cleaned)
        cleaned = re.sub(r"([A-Za-z])([اآ-ی])", r"\1 \2", cleaned)
        cleaned = re.sub(r"([اآ-ی])([A-Za-z])", r"\1 \2", cleaned)
        cleaned = cleaned.replace(" .", ".").replace(" ،", "،")
        sentence_breaks = re.split(r"(?<=[.!?؟])\s+", cleaned)
        paragraphs: list[str] = []
        for index in range(0, len(sentence_breaks), 3):
            paragraphs.append(" ".join(sentence_breaks[index : index + 3]).strip())
        return "\n\n".join(paragraph for paragraph in paragraphs if paragraph)
