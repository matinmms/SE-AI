from __future__ import annotations

import re
from collections import Counter
from typing import Any

from django.conf import settings

from apps.audio_pipeline.transcript_text import unwrap_embedded_transcript_json, unwrap_transcript_text
from apps.lectures.models import Lecture


class NoteGenerationInputComposer:
    greeting_pattern = re.compile(
        r"\b(سلام|خداحافظ|خسته نباشید|صبح بخیر|عصر بخیر|شب بخیر|hello|goodbye|bye)\b",
        re.IGNORECASE,
    )
    token_pattern = re.compile(r"[\w\u0600-\u06FF/+.#-]{3,}", re.IGNORECASE)
    heading_splitter = re.compile(r"[\n\r]+")
    bullet_prefixes = ("•", "-", "*", "▪", "◦", "➜", "–")
    filler_terms = {
        "جلسه",
        "عنوان",
        "درس",
        "استاد",
        "today",
        "lecture",
        "session",
        "slide",
        "page",
        "chapter",
        "section",
        "example",
        "topic",
    }

    def build(self, *, lecture: Lecture, transcript_override: str | None = None) -> dict[str, Any]:
        transcript_chunks = list(lecture.transcript_chunks.all().order_by("chunk_index"))
        slide_pages = list(lecture.slide_pages.all().order_by("page_number"))
        override_text = str(transcript_override or "").strip()
        if override_text:
            transcript_text = override_text
            transcript_chunk_payload = [
                {
                    "chunk_index": 0,
                    "text": override_text,
                    "start_ts": 0.0,
                    "end_ts": 0.0,
                }
            ]
        else:
            raw_transcript_chunk_payload = [
                {
                    "chunk_index": chunk.chunk_index,
                    "text": unwrap_transcript_text(chunk.text),
                    "start_ts": chunk.start_ts,
                    "end_ts": chunk.end_ts,
                }
                for chunk in transcript_chunks
                if unwrap_transcript_text(chunk.text).strip()
            ]
            transcript_chunk_payload, transcript_overlap_audit = self._trim_overlapping_transcript_chunks(
                chunks=raw_transcript_chunk_payload
            )
            transcript_text = "\n\n".join(
                chunk["text"].strip() for chunk in transcript_chunk_payload if chunk["text"].strip()
            ).strip()
            if not transcript_text:
                transcript_text = unwrap_embedded_transcript_json(lecture.merged_transcript).strip()
            if not transcript_chunk_payload and transcript_text:
                transcript_chunk_payload = [
                    {
                        "chunk_index": 0,
                        "text": transcript_text,
                        "start_ts": 0.0,
                        "end_ts": 0.0,
                    }
                ]
                transcript_overlap_audit = {
                    "trimmed_overlap_count": 0,
                    "duplicated_character_ranges": [],
                    "timestamp_overlap_count": 0,
                }
        if override_text:
            transcript_overlap_audit = {
                "trimmed_overlap_count": 0,
                "duplicated_character_ranges": [],
                "timestamp_overlap_count": 0,
            }
        slide_digest = self._build_slide_digest(slide_pages=slide_pages)
        course_hints = self._build_course_hints(lecture=lecture)
        concept_inventory = self._build_concept_inventory(
            transcript_chunk_payload=transcript_chunk_payload,
            slide_digest=slide_digest,
            course_hints=course_hints,
        )

        return {
            "lecture": {
                "id": str(lecture.id),
                "title": lecture.title,
                "course_name": lecture.course_name,
                "professor_name": lecture.professor_name,
                "input_language": lecture.input_language,
                "output_language": lecture.output_language,
                "note_detail_mode": lecture.note_detail_mode,
            },
            "transcript_text": transcript_text,
            "transcript_chunks": transcript_chunk_payload,
            "slide_context": slide_digest,
            "slide_source_name": self._slide_source_name(lecture) if slide_pages else "",
            "course_hints": course_hints,
            "concept_inventory": concept_inventory,
            "has_slides": bool(slide_digest["pages"]),
            "transcript_source": "override" if override_text else ("saved_chunks" if transcript_chunks else "merged_transcript"),
            "transcript_overlap_audit": transcript_overlap_audit,
        }

    def transcript_summary_chunks(self, *, transcript_text: str, max_chars: int) -> list[str]:
        cleaned = transcript_text.strip()
        if not cleaned:
            return []

        paragraphs = [segment.strip() for segment in re.split(r"\n{2,}", cleaned) if segment.strip()]
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for paragraph in paragraphs:
            para_len = len(paragraph)
            if current and current_len + para_len + 2 > max_chars:
                chunks.append("\n\n".join(current))
                current = [paragraph]
                current_len = para_len
            else:
                current.append(paragraph)
                current_len += para_len + (2 if current_len else 0)

        if current:
            chunks.append("\n\n".join(current))
        return chunks

    def transcript_semantic_batches(
        self,
        *,
        transcript_chunks: list[dict[str, Any]],
        transcript_text: str,
        max_chars: int | None = None,
    ) -> list[dict[str, Any]]:
        limit = max_chars or settings.NOTE_SEMANTIC_BATCH_CHARS
        if limit <= 0:
            limit = 8000

        if transcript_chunks:
            batches: list[dict[str, Any]] = []
            current_chunks: list[dict[str, Any]] = []
            current_parts: list[str] = []
            current_len = 0
            previous_keywords: set[str] = set()

            for chunk in transcript_chunks:
                text = str(chunk.get("text", "") or "").strip()
                if not text:
                    continue
                if len(text) > limit:
                    if current_parts:
                        batches.append(self._build_batch_payload(current_chunks=current_chunks, current_parts=current_parts))
                        current_chunks = []
                        current_parts = []
                        current_len = 0
                    split_parts = self._split_long_text(text=text, max_chars=limit)
                    split_durations = self._split_time_ranges_for_text_parts(
                        chunk=chunk,
                        parts=split_parts,
                    )
                    for split_part, (split_start_ts, split_end_ts) in zip(split_parts, split_durations, strict=False):
                        split_chunk = dict(chunk)
                        split_chunk["text"] = split_part
                        split_chunk["start_ts"] = split_start_ts
                        split_chunk["end_ts"] = split_end_ts
                        batches.append(
                            self._build_batch_payload(current_chunks=[split_chunk], current_parts=[split_part])
                        )
                    previous_keywords = self._concept_keywords(text)
                    continue
                keyword_shift = self._batch_boundary_shift(previous_keywords=previous_keywords, text=text)
                addition_len = len(text) + (2 if current_parts else 0)
                should_split = bool(current_parts and current_len + addition_len > limit)
                if current_parts and not should_split and current_len >= int(limit * 0.55) and keyword_shift >= 0.72:
                    should_split = True
                if should_split:
                    batches.append(self._build_batch_payload(current_chunks=current_chunks, current_parts=current_parts))
                    current_chunks = [chunk]
                    current_parts = [text]
                    current_len = len(text)
                else:
                    current_chunks.append(chunk)
                    current_parts.append(text)
                    current_len += addition_len
                previous_keywords = self._concept_keywords(text)

            if current_parts:
                batches.append(self._build_batch_payload(current_chunks=current_chunks, current_parts=current_parts))
            if batches:
                return batches

        text_batches = self._split_long_text(text=transcript_text, max_chars=limit)
        return [
            {
                "batch_index": index,
                "chunk_indexes": [index],
                "start_ts": 0.0,
                "end_ts": 0.0,
                "text": batch_text,
            }
            for index, batch_text in enumerate(text_batches)
        ]

    def _trim_overlapping_transcript_chunks(
        self,
        *,
        chunks: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cleaned_chunks: list[dict[str, Any]] = []
        duplicated_ranges: list[dict[str, Any]] = []
        timestamp_overlap_count = 0

        for chunk in chunks:
            current = dict(chunk)
            current["text"] = str(current.get("text", "") or "").strip()
            if not current["text"]:
                continue

            if cleaned_chunks:
                previous = cleaned_chunks[-1]
                previous_end = float(previous.get("end_ts", 0.0) or 0.0)
                current_start = float(current.get("start_ts", 0.0) or 0.0)
                if current_start < previous_end:
                    timestamp_overlap_count += 1
                    current["start_ts"] = previous_end

                overlap_chars = self._suffix_prefix_overlap_length(
                    left=str(previous.get("text", "") or ""),
                    right=str(current.get("text", "") or ""),
                )
                if overlap_chars >= 40:
                    duplicated_ranges.append(
                        {
                            "previous_chunk_index": previous.get("chunk_index", 0),
                            "current_chunk_index": current.get("chunk_index", 0),
                            "previous_char_range": [
                                max(0, len(str(previous.get("text", "") or "")) - overlap_chars),
                                len(str(previous.get("text", "") or "")),
                            ],
                            "current_char_range": [0, overlap_chars],
                            "duplicate_char_count": overlap_chars,
                            "reason": "suffix_prefix_text_overlap",
                        }
                    )
                    current["text"] = current["text"][overlap_chars:].lstrip()

            if current["text"]:
                cleaned_chunks.append(current)

        return cleaned_chunks, {
            "trimmed_overlap_count": len(duplicated_ranges),
            "duplicated_character_ranges": duplicated_ranges[:80],
            "timestamp_overlap_count": timestamp_overlap_count,
        }

    def _suffix_prefix_overlap_length(self, *, left: str, right: str) -> int:
        left_normalized = re.sub(r"\s+", " ", str(left or "")).strip()
        right_normalized = re.sub(r"\s+", " ", str(right or "")).strip()
        max_len = min(len(left_normalized), len(right_normalized), 1200)
        for length in range(max_len, 39, -1):
            left_part = left_normalized[-length:]
            right_part = right_normalized[:length]
            if left_part == right_part:
                return length
        return 0

    def _build_batch_payload(
        self,
        *,
        current_chunks: list[dict[str, Any]],
        current_parts: list[str],
    ) -> dict[str, Any]:
        return {
            "batch_index": len(current_chunks) and int(current_chunks[0].get("chunk_index", 0)) or 0,
            "chunk_indexes": [int(chunk.get("chunk_index", 0)) for chunk in current_chunks],
            "start_ts": float(current_chunks[0].get("start_ts", 0.0)) if current_chunks else 0.0,
            "end_ts": float(current_chunks[-1].get("end_ts", 0.0)) if current_chunks else 0.0,
            "text": "\n\n".join(current_parts).strip(),
        }

    def _split_long_text(self, *, text: str, max_chars: int) -> list[str]:
        cleaned = str(text or "").strip()
        if not cleaned:
            return []
        paragraphs = [segment.strip() for segment in re.split(r"\n{2,}", cleaned) if segment.strip()]
        if len(paragraphs) <= 1:
            paragraphs = [segment.strip() for segment in re.split(r"(?<=[\.\!\؟])\s+", cleaned) if segment.strip()]

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0
        for paragraph in paragraphs:
            para_len = len(paragraph)
            if current and current_len + para_len + 2 > max_chars:
                chunks.append("\n\n".join(current).strip())
                current = [paragraph]
                current_len = para_len
            else:
                current.append(paragraph)
                current_len += para_len + (2 if current_len else 0)

        if current:
            chunks.append("\n\n".join(current).strip())
        return chunks

    def _split_time_ranges_for_text_parts(
        self,
        *,
        chunk: dict[str, Any],
        parts: list[str],
    ) -> list[tuple[float, float]]:
        start_ts = float(chunk.get("start_ts", 0.0) or 0.0)
        end_ts = float(chunk.get("end_ts", 0.0) or 0.0)
        if not parts or end_ts <= start_ts:
            return [(start_ts, end_ts) for _part in parts]
        total_chars = sum(max(1, len(part)) for part in parts)
        duration = end_ts - start_ts
        ranges: list[tuple[float, float]] = []
        cursor = start_ts
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                part_end = end_ts
            else:
                part_duration = duration * (max(1, len(part)) / max(1, total_chars))
                part_end = min(end_ts, cursor + part_duration)
            ranges.append((round(cursor, 3), round(part_end, 3)))
            cursor = part_end
        return ranges

    @staticmethod
    def _slide_source_name(lecture: Lecture) -> str:
        from apps.uploads.models import LectureFileType, UploadStatus

        names: list[str] = []
        files = lecture.files.filter(
            file_type=LectureFileType.SLIDES, upload_status=UploadStatus.UPLOADED
        ).order_by("created_at")
        for lecture_file in files[:2]:
            stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", str(lecture_file.original_name or "").strip())
            stem = re.sub(r"[_\s]+", " ", stem).strip()
            if stem and stem not in names:
                names.append(stem[:80])
        return " · ".join(names)

    def _build_slide_digest(self, *, slide_pages: list[Any]) -> dict[str, Any]:
        pages: list[dict[str, Any]] = []
        for page in slide_pages:
            page_text_parts = [
                page.structured_text.strip(),
                page.text.strip(),
                page.ocr_text.strip(),
            ]
            formulas = [
                formula.get("latex", "")
                for formula in (page.formulas or [])
                if isinstance(formula, dict) and formula.get("latex")
            ]
            tables = [table for table in (page.tables or []) if isinstance(table, dict) and table.get("data")]
            flattened_tables = [self._flatten_table(table.get("data", [])) for table in tables if table.get("data")]
            full_text = "\n".join(part for part in page_text_parts if part).strip()
            heading_lines, bullet_lines = self._extract_slide_structure(full_text)
            keywords = self._top_keywords(full_text, limit=18)
            candidate_concepts = self._candidate_concepts(
                heading_lines=heading_lines,
                bullet_lines=bullet_lines,
                keywords=keywords,
                formulas=formulas,
                tables=flattened_tables,
            )
            pages.append(
                {
                    "page_number": page.page_number,
                    "text": full_text,
                    "heading_lines": heading_lines,
                    "bullet_lines": bullet_lines,
                    "formulas": formulas,
                    "formula_text": formulas,
                    "tables": [table.get("data", []) for table in tables],
                    "table_text": flattened_tables,
                    "keywords": keywords,
                    "candidate_concepts": candidate_concepts,
                }
            )
        return {"pages": pages}

    def _build_course_hints(self, *, lecture: Lecture) -> dict[str, Any]:
        tokens = re.findall(r"[\w\u0600-\u06FF/+.-]+", " ".join([lecture.course_name, lecture.title]).lower())
        filtered = []
        for token in tokens:
            token = token.strip("-_.")
            if len(token) < 3:
                continue
            if token in {"lesson", "lecture", "session", "course", "title", "test"}:
                continue
            filtered.append(token)
        unique = list(dict.fromkeys(filtered))
        return {
            "keywords": unique[:20],
            "professor_name": lecture.professor_name,
            "topic_hint": lecture.course_name or lecture.title,
        }

    def likely_housekeeping_ratio(self, *, text: str) -> float:
        if not text.strip():
            return 0.0
        matches = len(self.greeting_pattern.findall(text))
        word_count = max(1, len(text.split()))
        return round(matches / word_count, 4)

    def _batch_boundary_shift(self, *, previous_keywords: set[str], text: str) -> float:
        if not previous_keywords:
            return 0.0
        current_keywords = self._concept_keywords(text)
        if not current_keywords:
            return 0.0
        overlap = len(previous_keywords & current_keywords)
        union = max(1, len(previous_keywords | current_keywords))
        return 1.0 - (overlap / union)

    def _concept_keywords(self, text: str) -> set[str]:
        return set(self._top_keywords(text, limit=14))

    def _extract_slide_structure(self, text: str) -> tuple[list[str], list[str]]:
        heading_lines: list[str] = []
        bullet_lines: list[str] = []
        for raw_line in self.heading_splitter.split(text or ""):
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                continue
            is_bullet = line.startswith(self.bullet_prefixes)
            normalized = line.lstrip("".join(self.bullet_prefixes)).strip()
            if is_bullet and normalized:
                bullet_lines.append(normalized)
                continue
            token_count = len(normalized.split())
            if token_count <= 8 or normalized.endswith(":"):
                heading_lines.append(normalized.rstrip(":"))
        return heading_lines[:8], bullet_lines[:20]

    def _flatten_table(self, rows: list[Any]) -> str:
        flattened_rows: list[str] = []
        for row in rows:
            if not isinstance(row, list):
                continue
            cells = [re.sub(r"\s+", " ", str(cell or "")).strip() for cell in row]
            cells = [cell for cell in cells if cell]
            if cells:
                flattened_rows.append(" | ".join(cells))
        return "\n".join(flattened_rows).strip()

    def _top_keywords(self, text: str, *, limit: int) -> list[str]:
        tokens = []
        for token in self.token_pattern.findall(text or ""):
            normalized = token.lower().strip("-_.")
            if len(normalized) < 3 or normalized in self.filler_terms:
                continue
            if normalized.isdigit():
                continue
            tokens.append(normalized)
        counts = Counter(tokens)
        return [token for token, _count in counts.most_common(limit)]

    def _candidate_concepts(
        self,
        *,
        heading_lines: list[str],
        bullet_lines: list[str],
        keywords: list[str],
        formulas: list[str],
        tables: list[str],
    ) -> list[str]:
        concepts: list[str] = []
        concepts.extend(line for line in heading_lines if line)
        concepts.extend(line for line in bullet_lines[:8] if 2 <= len(line.split()) <= 8)
        concepts.extend(keyword for keyword in keywords[:8] if keyword)
        if formulas:
            concepts.append("فرمول‌ها و روابط")
        if tables:
            concepts.append("جدول‌ها و مقایسه‌ها")
        return list(dict.fromkeys(concepts))[:14]

    def _build_concept_inventory(
        self,
        *,
        transcript_chunk_payload: list[dict[str, Any]],
        slide_digest: dict[str, Any],
        course_hints: dict[str, Any],
    ) -> list[dict[str, Any]]:
        transcript_text = "\n".join(str(chunk.get("text", "") or "") for chunk in transcript_chunk_payload)
        transcript_keywords = set(self._top_keywords(transcript_text, limit=40))
        inventory: list[dict[str, Any]] = []

        for page in slide_digest.get("pages", []):
            if not isinstance(page, dict):
                continue
            page_number = int(page.get("page_number") or 0)
            for concept in page.get("candidate_concepts", []):
                title = str(concept or "").strip()
                if not title:
                    continue
                tokens = set(self._top_keywords(title, limit=10))
                tokens.update(self._top_keywords(str(page.get("text", "") or ""), limit=12))
                prominence = 1.0
                if title in page.get("heading_lines", []):
                    prominence += 1.5
                if title in page.get("bullet_lines", []):
                    prominence += 0.5
                transcript_overlap = len(tokens & transcript_keywords)
                inventory.append(
                    {
                        "title": title,
                        "normalized_title": self._normalize_concept_key(title),
                        "page_numbers": [page_number] if page_number else [],
                        "tokens": sorted(tokens)[:18],
                        "source": "slide",
                        "prominence": round(prominence + min(2.0, transcript_overlap / 2), 2),
                        "transcript_overlap": transcript_overlap,
                    }
                )

        hint_tokens = [token for token in course_hints.get("keywords", []) if len(token) >= 3]
        for token in hint_tokens[:10]:
            inventory.append(
                {
                    "title": token,
                    "normalized_title": self._normalize_concept_key(token),
                    "page_numbers": [],
                    "tokens": [token],
                    "source": "hint",
                    "prominence": 0.4,
                    "transcript_overlap": int(token in transcript_keywords),
                }
            )

        deduped: dict[str, dict[str, Any]] = {}
        for item in inventory:
            key = item["normalized_title"]
            if not key:
                continue
            existing = deduped.get(key)
            if existing is None:
                deduped[key] = item
                continue
            existing["page_numbers"] = sorted(set(existing["page_numbers"]) | set(item["page_numbers"]))
            existing["tokens"] = sorted(set(existing["tokens"]) | set(item["tokens"]))[:20]
            existing["prominence"] = max(existing["prominence"], item["prominence"])
            existing["transcript_overlap"] = max(existing["transcript_overlap"], item["transcript_overlap"])
            if len(item["title"]) > len(existing["title"]):
                existing["title"] = item["title"]

        ranked = sorted(
            deduped.values(),
            key=lambda item: (item["prominence"], item["transcript_overlap"], len(item["tokens"])),
            reverse=True,
        )
        return ranked[:60]

    def _normalize_concept_key(self, text: str) -> str:
        normalized_tokens = []
        for token in self.token_pattern.findall(text or ""):
            cleaned = token.lower().strip("-_.")
            if len(cleaned) < 3 or cleaned in self.filler_terms:
                continue
            normalized_tokens.append(cleaned)
        return "-".join(normalized_tokens[:8])
