from __future__ import annotations

import re
from collections import Counter
from copy import deepcopy
from typing import Any

from apps.lectures.models import Lecture
from apps.notes.renderers import canonical_semantic_block_type, is_summary_semantic_block


class SemanticNoteEnricher:
    sentence_splitter = re.compile(r"(?<=[\.\!\؟])\s+|[\n\r]+")
    token_pattern = re.compile(r"[\w\u0600-\u06FF/+.-]{3,}", re.IGNORECASE)
    filler_cleanup_patterns = [
        (re.compile(r"(^|[\.!\؟]\s+)(خب|ببینید|حالا)\s+", re.IGNORECASE), r"\1"),
        (re.compile(r"(^|[\.!\؟]\s+)(در\s+واقع|یعنی|به\s+اصطلاح)\s+", re.IGNORECASE), r"\1"),
        (re.compile(r"\s{2,}"), " "),
    ]
    note_markers = ("نکته", "توجه", "دقت", "مهم", "باید", "لازم", "ضروری", "در نتیجه")
    example_markers = ("مثال", "مثلا", "برای مثال", "فرض کنید", "اگر ")
    figure_markers = ("شکل", "نمودار", "تصویر", "دیاگرام", "diagram", "figure")
    table_markers = ("جدول", "table")

    def enrich_document(
        self,
        *,
        document: dict[str, Any],
        lecture: Lecture,
        context: dict[str, Any],
        synthesize_blocks: bool = True,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        enriched = deepcopy(document)
        pages = enriched.get("pages")
        if not isinstance(pages, list) or not pages:
            return document, {"before": {}, "after": {}, "actions": []}

        actions: list[str] = []
        before = self._structure_report(pages)
        transcript_sentences = self._extract_sentences(str(context.get("transcript_text", "")))

        for page in pages:
            blocks = page.get("blocks", [])
            if not isinstance(blocks, list):
                page["blocks"] = []
                continue
            page["blocks"] = self._normalize_blocks(blocks)

        self._rebalance_headers(pages=pages, actions=actions)
        self._attach_slide_references(pages=pages, context=context, actions=actions)

        first_page = pages[0]
        first_blocks = first_page.get("blocks", [])
        self._ensure_lead_paragraph(blocks=first_blocks, actions=actions)
        if not synthesize_blocks:
            # The teaching pipeline writes its own intro, notes, examples and
            # summary; boxes assembled from raw transcript sentences would pull
            # the lecturer's wording back into a deliberately re-taught note.
            self._consolidate_summary_boxes(pages=pages, actions=actions)
            after = self._structure_report(pages)
            return enriched, {"before": before, "after": after, "actions": actions}
        self._ensure_intro_section(blocks=first_blocks, actions=actions)
        summarized_mode = lecture.note_detail_mode == "summarized"

        if not summarized_mode and not self._has_block_type(pages=pages, block_type="note-box"):
            note_content = self._pick_note_content(transcript_sentences=transcript_sentences, pages=pages)
            if note_content:
                insert_at = self._find_insertion_index(first_blocks)
                first_blocks.insert(
                    insert_at,
                    {
                        "type": "note-box",
                        "title": "نکته کلیدی",
                        "content": note_content,
                    },
                )
                actions.append("inserted_note_box")

        if not summarized_mode and not self._has_block_type(pages=pages, block_type="key-highlight"):
            key_content = self._pick_key_highlight(transcript_sentences=transcript_sentences, pages=pages)
            if key_content:
                insert_at = min(len(first_blocks), self._find_insertion_index(first_blocks) + 1)
                first_blocks.insert(insert_at, {"type": "key-highlight", "content": key_content})
                actions.append("inserted_key_highlight")

        if not summarized_mode and not self._has_block_type(pages=pages, block_type="example-box"):
            example_content = self._pick_example_content(transcript_sentences=transcript_sentences, pages=pages)
            if example_content:
                target_page = pages[min(len(pages) - 1, 0)]
                target_blocks = target_page.get("blocks", [])
                example_index = self._find_example_insertion_index(target_blocks)
                target_blocks.insert(
                    example_index,
                    {
                        "type": "example-box",
                        "title": "مثال",
                        "content": example_content,
                    },
                )
                actions.append("inserted_example_box")

        if not self._has_block_type(pages=pages, block_type="summary-box"):
            summary_items = self._build_summary_items(pages=pages, transcript_sentences=transcript_sentences)
            if summary_items:
                pages[-1].setdefault("blocks", []).append(
                    {
                        "type": "summary-box",
                        "title": "جمع‌بندی",
                        "icon": "📌",
                        "items": summary_items,
                    }
                )
                actions.append("inserted_summary_box")

        self._consolidate_summary_boxes(pages=pages, actions=actions)

        after = self._structure_report(pages)
        return enriched, {"before": before, "after": after, "actions": actions}

    def _attach_slide_references(self, *, pages: list[dict[str, Any]], context: dict[str, Any], actions: list[str]) -> None:
        slide_pages = context.get("slide_context", {}).get("pages", [])
        if not isinstance(slide_pages, list) or not slide_pages:
            return

        prepared_pages: list[dict[str, Any]] = []
        for slide_page in slide_pages:
            if not isinstance(slide_page, dict):
                continue
            page_number = slide_page.get("page_number")
            if page_number in {None, ""}:
                continue
            page_text = "\n".join(
                [
                    str(slide_page.get("text", "") or "").strip(),
                    " ".join(str(item) for item in slide_page.get("heading_lines", []) if str(item).strip()),
                    " ".join(str(item) for item in slide_page.get("bullet_lines", []) if str(item).strip()),
                    " ".join(str(item) for item in slide_page.get("keywords", []) if str(item).strip()),
                    " ".join(str(item) for item in slide_page.get("candidate_concepts", []) if str(item).strip()),
                    " ".join(str(item) for item in slide_page.get("formulas", []) if str(item).strip()),
                    " ".join(" ".join(str(cell) for cell in row) for row in slide_page.get("tables", []) if isinstance(row, list)),
                    " ".join(str(item) for item in slide_page.get("table_text", []) if str(item).strip()),
                ]
            ).strip()
            prepared_pages.append(
                {
                    "page_number": page_number,
                    "text": page_text,
                    "tokens": set(self._extract_tokens(page_text)),
                    "has_table": bool(slide_page.get("tables")),
                    "has_formula": bool(slide_page.get("formulas")),
                    "has_headings": bool(slide_page.get("heading_lines")),
                }
            )

        if not prepared_pages:
            return

        attached_count = 0
        for page in pages:
            for block in page.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                field_name = self._reference_field_for_block(block)
                if not field_name:
                    continue
                if field_name == "items":
                    raw_items = block.get("items", [])
                    existing_items = [
                        self._normalized_item_text(item)
                        for item in raw_items
                        if self._normalized_item_text(item)
                    ]
                    existing_text = " ".join(existing_items).strip()
                else:
                    existing_text = str(block.get(field_name, "") or "").strip()
                existing_html = str(block.get(f"{field_name}_html", "") or "").strip()
                combined = " ".join(
                    part for part in [str(block.get("title", "") or "").strip(), existing_text, existing_html] if part
                )
                if not combined or "[[slide:" in combined or "ref-note--slide" in combined:
                    continue
                best_page = self._match_slide_page(block=block, text=combined, prepared_pages=prepared_pages)
                if best_page is None:
                    continue
                marker = f" [[slide:{best_page}]]"
                if field_name == "items":
                    if existing_items:
                        block["items"] = [
                            f"{item}{marker}".strip() if item else marker.strip()
                            for item in existing_items
                        ]
                    else:
                        block["items"] = [marker.strip()]
                else:
                    block[field_name] = f"{existing_text}{marker}".strip() if existing_text else marker.strip()
                if block.get("type") == "figure-reference" and not str(block.get("number", "") or "").strip():
                    block["number"] = "ارجاع اسلاید"
                attached_count += 1

        if attached_count:
            actions.append(f"attached_slide_references:{attached_count}")

    def _reference_field_for_block(self, block: dict[str, Any]) -> str | None:
        block_type = str(block.get("type", "") or "")
        if block_type == "figure-reference":
            return "caption"
        if block_type == "table-container":
            return "caption"
        if block_type in {"paragraph", "note-box", "example-box", "key-highlight", "exam-point", "summary-box"}:
            return "content"
        if block_type == "list":
            return "items"
        return None

    def _match_slide_page(
        self,
        *,
        block: dict[str, Any],
        text: str,
        prepared_pages: list[dict[str, Any]],
    ) -> int | None:
        block_text = text.strip()
        lowered = block_text.lower()
        wants_figure = any(marker in lowered for marker in self.figure_markers)
        wants_table = any(marker in lowered for marker in self.table_markers)
        block_type = str(block.get("type", "") or "")
        generic_grounding = block_type in {
            "paragraph",
            "note-box",
            "example-box",
            "key-highlight",
            "exam-point",
            "list",
            "summary-box",
        }
        if not wants_figure and not wants_table and not generic_grounding and block_type not in {"figure-reference", "table-container"}:
            return None

        block_tokens = set(self._extract_tokens(block_text))
        best_page_number: int | None = None
        best_score = 0
        second_best_score = 0

        for slide_page in prepared_pages:
            score = len(block_tokens & slide_page["tokens"])
            if wants_table and slide_page["has_table"]:
                score += 6
            if wants_figure:
                score += 2
                if slide_page["has_formula"]:
                    score += 1
            if generic_grounding and slide_page["has_headings"]:
                score += 1
            if score > best_score:
                second_best_score = best_score
                best_score = score
                best_page_number = int(slide_page["page_number"])
            elif score > second_best_score:
                second_best_score = score

        if best_page_number is None:
            return None
        threshold = 3 if wants_figure or wants_table else 4
        if best_score < threshold:
            return None
        if generic_grounding and second_best_score and best_score - second_best_score < 2:
            return None
        return best_page_number

    def _extract_tokens(self, text: str) -> list[str]:
        return [
            token.lower()
            for token in self.token_pattern.findall(text or "")
            if len(token.strip("-_.")) >= 3
        ]

    def _normalize_blocks(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            current = dict(block)
            block_type = canonical_semantic_block_type(
                current.get("type") or current.get("block_type") or current.get("component")
            )
            if is_summary_semantic_block(current):
                block_type = "summary-box"
            current["type"] = block_type
            if block_type in {
                "paragraph",
                "note-box",
                "example-box",
                "unrelated-discussion-box",
                "key-highlight",
                "exam-point",
            }:
                field = "content"
                if block_type == "key-highlight":
                    field = "content"
                content = str(current.get(field, "") or "").strip()
                if content:
                    current[field] = self._polish_text(content)
            if block_type == "list":
                items = current.get("items")
                if isinstance(items, list):
                    current["items"] = [self._polish_text(self._normalized_item_text(item)) for item in items if self._normalized_item_text(item)]
            if block_type == "paragraph":
                normalized.extend(self._split_long_paragraph(current))
            elif block_type == "summary-box":
                items = current.get("items")
                if isinstance(items, str):
                    current["items"] = [self._polish_text(items)]
                elif isinstance(items, list):
                    current["items"] = [self._polish_text(str(item)) for item in items if str(item).strip()]
                normalized.append(current)
            else:
                normalized.append(current)
        return normalized

    def _consolidate_summary_boxes(self, *, pages: list[dict[str, Any]], actions: list[str]) -> None:
        summary_blocks: list[dict[str, Any]] = []
        original_final_block = None
        if pages and isinstance(pages[-1].get("blocks"), list) and pages[-1]["blocks"]:
            original_final_block = pages[-1]["blocks"][-1]
        for page in pages:
            blocks = page.get("blocks", [])
            if not isinstance(blocks, list):
                continue
            retained: list[dict[str, Any]] = []
            for block in blocks:
                if is_summary_semantic_block(block):
                    block["type"] = "summary-box"
                    summary_blocks.append(block)
                else:
                    retained.append(block)
            page["blocks"] = retained

        if not summary_blocks:
            return

        merged = dict(summary_blocks[0])
        merged_items: list[str] = []
        merged_content: list[str] = []
        seen: set[str] = set()

        def add_item(value: Any) -> None:
            text = self._polish_text(self._normalized_item_text(value))
            signature = " ".join(text.split()).casefold()
            if text and signature not in seen:
                seen.add(signature)
                merged_items.append(text)

        for block in summary_blocks:
            if not str(merged.get("title", "") or "").strip() and str(block.get("title", "") or "").strip():
                merged["title"] = block["title"]
            if not str(merged.get("icon", "") or "").strip() and str(block.get("icon", "") or "").strip():
                merged["icon"] = block["icon"]

            raw_items = block.get("items", [])
            if isinstance(raw_items, list):
                for item in raw_items:
                    add_item(item)
            elif isinstance(raw_items, str):
                add_item(raw_items)

            content = self._polish_text(str(block.get("content", "") or ""))
            if not content:
                continue
            # A content-only trailing summary is normally the final takeaway emitted
            # separately by the model. Keep it inside the one summary list.
            if not raw_items:
                add_item(content)
                continue
            signature = " ".join(content.split()).casefold()
            if signature not in seen:
                seen.add(signature)
                merged_content.append(content)

        merged["items"] = merged_items
        merged["content"] = "\n".join(merged_content)
        pages[-1].setdefault("blocks", []).append(merged)

        if len(summary_blocks) > 1:
            actions.append(f"consolidated_summary_boxes:{len(summary_blocks)}")
        elif summary_blocks[0] is not original_final_block:
            actions.append("moved_summary_box_to_document_end")

    def _normalized_item_text(self, item: Any) -> str:
        if isinstance(item, dict):
            for key in ("text", "content", "title", "label", "value"):
                candidate = str(item.get(key, "") or "").strip()
                if candidate:
                    return candidate
            return " ".join(str(value).strip() for value in item.values() if str(value).strip()).strip()
        return str(item or "").strip()

    def _split_long_paragraph(self, block: dict[str, Any]) -> list[dict[str, Any]]:
        content = str(block.get("content", "") or "").strip()
        if len(content) < 520:
            return [block]
        sentences = self._extract_sentences(content)
        if len(sentences) < 4:
            return [block]

        parts: list[str] = []
        current: list[str] = []
        current_len = 0
        for sentence in sentences:
            if current and current_len + len(sentence) > 360:
                parts.append(" ".join(current).strip())
                current = [sentence]
                current_len = len(sentence)
            else:
                current.append(sentence)
                current_len += len(sentence)
        if current:
            parts.append(" ".join(current).strip())

        split_blocks: list[dict[str, Any]] = []
        for index, part in enumerate(parts):
            next_block = dict(block)
            next_block["content"] = part
            if index > 0:
                variants = next_block.get("variants")
                if isinstance(variants, list) and "lead" in variants:
                    next_block["variants"] = [variant for variant in variants if variant != "lead"]
            split_blocks.append(next_block)
        return split_blocks

    def _rebalance_headers(self, *, pages: list[dict[str, Any]], actions: list[str]) -> None:
        chapter_blocks: list[dict[str, Any]] = []
        for page in pages:
            for block in page.get("blocks", []):
                if isinstance(block, dict) and block.get("type") == "chapter-header":
                    chapter_blocks.append(block)
        has_section = self._has_block_type(pages=pages, block_type="section-header")
        if len(chapter_blocks) <= 1 or has_section:
            return

        for index, block in enumerate(chapter_blocks[1:], start=1):
            block["type"] = "section-header"
            block["number"] = self._to_persian_number(index)
            block.pop("subtitle", None)
        actions.append("converted_extra_chapter_headers_to_sections")

    def _ensure_lead_paragraph(self, *, blocks: list[dict[str, Any]], actions: list[str]) -> None:
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "paragraph":
                continue
            variants = block.get("variants")
            if not isinstance(variants, list):
                variants = []
            if "lead" not in variants:
                block["variants"] = ["lead", *[variant for variant in variants if variant != "lead"]]
                actions.append("promoted_first_paragraph_to_lead")
            return

    def _ensure_intro_section(self, *, blocks: list[dict[str, Any]], actions: list[str]) -> None:
        has_introish_header = any(
            isinstance(block, dict)
            and block.get("type") in {"section-header", "subsection-header"}
            for block in blocks[:4]
        )
        if has_introish_header:
            return

        insert_at = 1 if blocks and blocks[0].get("type") == "chapter-header" else 0
        blocks.insert(
            insert_at,
            {
                "type": "section-header",
                "number": "۱",
                "title": "مقدمه و چارچوب بحث",
            },
        )
        actions.append("inserted_intro_section_header")

    def _find_insertion_index(self, blocks: list[dict[str, Any]]) -> int:
        paragraph_seen = False
        for index, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "paragraph":
                if paragraph_seen:
                    return index
                paragraph_seen = True
        return min(len(blocks), 3)

    def _find_example_insertion_index(self, blocks: list[dict[str, Any]]) -> int:
        for index, block in enumerate(blocks):
            if isinstance(block, dict) and block.get("type") in {"summary-box", "page-footer"}:
                return index
        return max(0, len(blocks) - 1)

    def _pick_note_content(self, *, transcript_sentences: list[str], pages: list[dict[str, Any]]) -> str:
        candidates = self._academic_candidates(
            transcript_sentences=transcript_sentences,
            pages=pages,
        )
        for sentence in candidates:
            lowered = sentence.lower()
            if any(marker in lowered for marker in self.note_markers) and 40 <= len(sentence) <= 220:
                return sentence
        for sentence in candidates:
            if 60 <= len(sentence) <= 180:
                return sentence
        return ""

    def _pick_key_highlight(self, *, transcript_sentences: list[str], pages: list[dict[str, Any]]) -> str:
        candidates = self._academic_candidates(
            transcript_sentences=transcript_sentences,
            pages=pages,
        )
        for sentence in candidates:
            if 35 <= len(sentence) <= 120 and ("است" in sentence or "می‌شود" in sentence or "یعنی" in sentence):
                return sentence
        return ""

    def _pick_example_content(self, *, transcript_sentences: list[str], pages: list[dict[str, Any]]) -> str:
        candidates = self._academic_candidates(
            transcript_sentences=transcript_sentences,
            pages=pages,
        )
        for index, sentence in enumerate(candidates):
            lowered = sentence.lower()
            if any(marker in lowered for marker in self.example_markers):
                # A worked example commonly spans setup, board work, and conclusion.
                # Keeping only the cue sentence was creating visibly truncated boxes.
                window: list[str] = []
                if index > 0 and len(candidates[index - 1]) <= 180:
                    window.append(candidates[index - 1])
                window.append(sentence)
                for following in candidates[index + 1 : index + 4]:
                    if len(following) > 260 or sum(len(part) for part in window) + len(following) > 700:
                        break
                    window.append(following)
                return self._polish_text(" ".join(window))
        return ""

    def _build_summary_items(self, *, pages: list[dict[str, Any]], transcript_sentences: list[str]) -> list[str]:
        titles: list[str] = []
        for page in pages:
            for block in page.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"chapter-header", "section-header", "subsection-header"}:
                    title = str(block.get("title", "") or "").strip()
                    if title and title not in {"مقدمه و چارچوب بحث"}:
                        titles.append(title)

        items = [self._polish_text(title) for title in titles[:4]]
        if len(items) < 3:
            for sentence in self._academic_candidates(
                transcript_sentences=transcript_sentences,
                pages=pages,
            ):
                cleaned = self._shorten_sentence(sentence, max_length=120)
                if cleaned and cleaned not in items:
                    items.append(cleaned)
                if len(items) >= 4:
                    break
        return items[:5]

    def _academic_candidates(
        self,
        *,
        transcript_sentences: list[str],
        pages: list[dict[str, Any]],
    ) -> list[str]:
        page_sentences = self._page_sentences(pages=pages)
        if self._has_block_type(pages=pages, block_type="unrelated-discussion-box"):
            # The raw transcript includes the deliberately isolated side
            # conversation. Do not let fallback enrichment move it back into a
            # course note, example, or summary.
            return page_sentences
        return page_sentences + transcript_sentences

    def _page_sentences(self, *, pages: list[dict[str, Any]]) -> list[str]:
        sentences: list[str] = []
        for page in pages:
            for block in page.get("blocks", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "unrelated-discussion-box":
                    continue
                for field in ("content", "subtitle", "title"):
                    value = str(block.get(field, "") or "").strip()
                    if not value:
                        continue
                    sentences.extend(self._extract_sentences(value))
        return sentences

    def _extract_sentences(self, text: str) -> list[str]:
        prepared = text.replace("\r", "\n").strip()
        if not prepared:
            return []
        sentences = [self._polish_text(part) for part in self.sentence_splitter.split(prepared) if part.strip()]
        return [sentence for sentence in sentences if len(sentence) >= 18]

    def _polish_text(self, text: str) -> str:
        cleaned = text.strip()
        for pattern, replacement in self.filler_cleanup_patterns:
            cleaned = pattern.sub(replacement, cleaned)
        cleaned = re.sub(r"\s+([،؛:,.!?؟])", r"\1", cleaned)
        cleaned = re.sub(r"\(\s+", "(", cleaned)
        cleaned = re.sub(r"\s+\)", ")", cleaned)
        return cleaned.strip()

    def _shorten_sentence(self, text: str, *, max_length: int) -> str:
        shortened = self._polish_text(text)
        if len(shortened) <= max_length:
            return shortened
        cut = shortened[: max_length - 1].rsplit(" ", 1)[0].strip()
        return f"{cut}…" if cut else shortened[: max_length - 1] + "…"

    def _structure_report(self, pages: list[dict[str, Any]]) -> dict[str, Any]:
        counter: Counter[str] = Counter()
        for page in pages:
            for block in page.get("blocks", []):
                if isinstance(block, dict):
                    counter[str(block.get("type", ""))] += 1
        return {
            "block_counts": dict(counter),
            "unique_block_types": len(counter),
            "has_note_box": counter.get("note-box", 0) > 0,
            "has_example_box": counter.get("example-box", 0) > 0,
            "has_summary_box": counter.get("summary-box", 0) > 0,
            "has_key_highlight": counter.get("key-highlight", 0) > 0,
            "has_unrelated_discussion_box": counter.get("unrelated-discussion-box", 0) > 0,
        }

    def _has_block_type(self, *, pages: list[dict[str, Any]], block_type: str) -> bool:
        return any(
            isinstance(block, dict) and block.get("type") == block_type
            for page in pages
            for block in page.get("blocks", [])
        )

    @staticmethod
    def _to_persian_number(value: int) -> str:
        digits = "۰۱۲۳۴۵۶۷۸۹"
        return "".join(digits[int(ch)] for ch in str(value))
