from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ParsedSlidePage:
    page_number: int
    text: str
    raw_extracted_text: str
    structured_text: str
    images: list[dict[str, object]] = field(default_factory=list)
    tables: list[dict[str, object]] = field(default_factory=list)
    formulas: list[dict[str, object]] = field(default_factory=list)
    ocr_text: str = ""
    thumbnail_path: str = ""


@dataclass
class ParsedSlidesDocument:
    pages: list[ParsedSlidePage]
    raw_pdf_text: list[dict[str, object]] = field(default_factory=list)
    ocr_output: list[dict[str, object]] = field(default_factory=list)
    extracted_tables: list[dict[str, object]] = field(default_factory=list)
    extracted_formulas: list[dict[str, object]] = field(default_factory=list)
    slide_pages_raw: list[dict[str, object]] = field(default_factory=list)
