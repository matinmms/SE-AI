from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from collections import Counter

import certifi
from django.conf import settings

from apps.lectures.models import Lecture


class HotwordGenerationService:
    fallback_terms = {
        "operating systems": ["kernel", "mutex", "scheduler", "deadlock", "process", "thread", "semaphore"],
        "advanced operating systems": ["kernel", "mutex", "scheduler", "deadlock", "process", "thread", "semaphore"],
        "network": ["TCP/IP", "DNS", "router", "latency", "packet", "bandwidth", "socket"],
        "algorithm": [
            "heap sort", "graph", "recurrence", "complexity", "array", "tree", "stack",
            "Big O", "O(n)", "O(n^2)", "O(log n)", "O(n log n)", "O(1)", "Omega", "Theta",
            "امگا", "تتا", "پیچیدگی زمانی", "نماد بزرگ O",
        ],
        "الگوریتم": [
            "heap sort", "graph", "recurrence", "complexity", "array", "tree", "stack",
            "Big O", "O(n)", "O(n^2)", "O(log n)", "O(n log n)", "O(1)", "Omega", "Theta",
            "امگا", "تتا", "پیچیدگی زمانی", "نماد بزرگ O",
        ],
        "math": ["ماتریس", "انتگرال", "همگرایی", "مشتق", "بردار", "حد", "تابع"],
        "physics": ["Fourier", "integral", "vector", "energy", "momentum", "ماتریس", "انتگرال"],
        "biology": ["mitochondria", "photosynthesis", "enzyme", "cell", "DNA", "RNA"],
        "computer": ["Kubernetes", "TCP/IP", "heap", "mutex", "process", "thread", "DNS"],
        "ریز پردازنده": ["ریزپردازنده", "پردازنده", "cpu", "alu", "register", "instruction", "interrupt"],
        "ریزپردازنده": ["ریزپردازنده", "پردازنده", "cpu", "alu", "register", "instruction", "interrupt"],
        "madar haye vaset": ["مدارهای واسط", "bus", "i/o", "interface", "gpio", "interrupt", "decoder"],
        "مدار های واسط": ["مدارهای واسط", "bus", "i/o", "interface", "gpio", "interrupt", "decoder"],
        "مدارهای واسط": ["مدارهای واسط", "bus", "i/o", "interface", "gpio", "interrupt", "decoder"],
        "riz pardazande": ["ریزپردازنده", "پردازنده", "cpu", "alu", "register", "instruction", "interrupt"],
        "microprocessor": ["microprocessor", "cpu", "alu", "register", "instruction", "interrupt", "assembly"],
        "interface circuits": ["interface", "bus", "gpio", "decoder", "address bus", "data bus", "i/o"],
    }

    persian_stopwords = {
        "برای", "است", "این", "اون", "های", "هایش", "شده", "هایی", "خود", "روی", "در", "از", "به", "با", "که",
        "را", "و", "یا", "تا", "یک", "هم", "می", "شود", "کرد", "کرده", "اگر", "اما", "بعد", "قبل", "روی", "بین",
    }
    generic_noise_terms = {
        "test", "week", "lecture", "class", "lesson", "session", "upload", "audio", "slides",
        "بدون", "عنوان", "درس", "جلسه", "شماره", "مهندسی",
    }

    def __init__(self) -> None:
        self.ssl_context = ssl.create_default_context(cafile=certifi.where())

    def _normalize_term(self, value: str) -> str:
        cleaned = re.sub(r"\s+", " ", value.strip())
        cleaned = cleaned.replace("‌", " ")
        cleaned = re.sub(r"[^\w\s/+\-#().²³⁰-⁹]", "", cleaned, flags=re.UNICODE)
        return cleaned[: settings.HOTWORDS_MAX_LENGTH].strip()

    def _normalized_haystack(self, value: str) -> str:
        cleaned = self._normalize_term(value).casefold()
        cleaned = cleaned.replace("-", " ")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    def _tokenize(self, value: str) -> list[str]:
        normalized = value.replace("\n", " ")
        # Parentheses and superscript digits are kept so complexity notation
        # such as "O(n)" or "O(n²)" survives as one token instead of being
        # shredded into single, too-short-to-keep characters that then get
        # filtered out by the length check below.
        candidates = re.findall(r"[\w/+.#()²³⁰-⁹-]{2,}", normalized, flags=re.UNICODE)
        terms: list[str] = []
        for candidate in candidates:
            normalized_candidate = self._normalize_term(candidate)
            if (
                len(normalized_candidate) < 2
                or normalized_candidate.casefold() in self.persian_stopwords
                or normalized_candidate.casefold() in self.generic_noise_terms
                or normalized_candidate.isdigit()
            ):
                continue
            terms.append(normalized_candidate)
        return terms

    def _slide_text(self, *, lecture: Lecture) -> str:
        parts: list[str] = []
        for page in lecture.slide_pages.all().order_by("page_number"):
            parts.extend(
                [
                    page.text or "",
                    page.structured_text or "",
                    page.ocr_text or "",
                    page.raw_extracted_text or "",
                ]
            )
            for formula in page.formulas:
                if isinstance(formula, dict):
                    parts.append(str(formula.get("latex", "")))
            for table in page.tables:
                if isinstance(table, dict):
                    parts.append(json.dumps(table, ensure_ascii=False))
        return "\n".join(part for part in parts if part.strip())

    def _metadata_text(self, *, lecture: Lecture) -> str:
        return "\n".join(
            part
            for part in [
                lecture.course_name.strip(),
                lecture.title.strip(),
                lecture.professor_name.strip(),
                f"course keywords: {lecture.course_name.strip()}",
                f"lecture topic: {lecture.title.strip()}",
            ]
            if part
        )

    def _dadmatech_keywords(self, *, text: str) -> list[str]:
        if not settings.DADMATECH_API_TOKEN or not text.strip():
            return []

        payload = json.dumps(
            {
                "text": text[:12000],
                "max_sentences": 3,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            url=f"{settings.DADMATECH_BASE_URL}/summarize/v1/summarize",
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "gateway-token": settings.DADMATECH_API_TOKEN,
            },
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=settings.GAPGPT_TRANSCRIPTION_TIMEOUT_SECONDS,
                context=self.ssl_context,
            ) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, ValueError):
            return []

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return []

        text_candidates: list[str] = []
        if isinstance(parsed, dict):
            for key in ("summary", "text", "result", "data"):
                value = parsed.get(key)
                if isinstance(value, str):
                    text_candidates.append(value)
                elif isinstance(value, list):
                    text_candidates.extend(str(item) for item in value)
                elif isinstance(value, dict):
                    text_candidates.extend(str(item) for item in value.values())
        return self._tokenize(" ".join(text_candidates))

    def _metadata_terms(self, *, lecture: Lecture) -> list[str]:
        haystack = f"{lecture.title} {lecture.course_name} {lecture.professor_name}".strip()
        terms = self._tokenize(haystack)
        key = self._normalized_haystack(f"{lecture.course_name} {lecture.title}")
        for topic, defaults in self.fallback_terms.items():
            if self._normalized_haystack(topic) in key:
                terms.extend(defaults)
        return terms

    def generate(self, *, lecture: Lecture) -> tuple[list[str], str, dict[str, object]]:
        slide_text = self._slide_text(lecture=lecture)
        metadata_text = self._metadata_text(lecture=lecture)
        slide_terms = self._tokenize(slide_text)
        summarized_terms = self._dadmatech_keywords(text=slide_text)
        metadata_terms = self._metadata_terms(lecture=lecture)
        metadata_summary_terms = self._dadmatech_keywords(text=metadata_text)

        counter: Counter[str] = Counter()
        for term in slide_terms:
            counter[term] += 3
        for term in summarized_terms:
            counter[term] += 5
        for term in metadata_terms:
            counter[term] += 2
        for term in metadata_summary_terms:
            counter[term] += 4

        combined: list[str] = []
        for term, _score in counter.most_common(settings.HOTWORDS_MAX_COUNT * 2):
            normalized = self._normalize_term(term)
            if normalized and normalized not in combined:
                combined.append(normalized)
            if len(combined) >= settings.HOTWORDS_MAX_COUNT:
                break

        if slide_text.strip() and combined:
            source = "slides"
        elif metadata_summary_terms:
            source = "course_summary"
        else:
            source = "course_metadata"
        debug = {
            "slide_terms": slide_terms[: settings.HOTWORDS_MAX_COUNT],
            "summarized_terms": summarized_terms[: settings.HOTWORDS_MAX_COUNT],
            "metadata_terms": metadata_terms[: settings.HOTWORDS_MAX_COUNT],
            "metadata_summary_terms": metadata_summary_terms[: settings.HOTWORDS_MAX_COUNT],
            "source": source,
        }
        return combined, source, debug
