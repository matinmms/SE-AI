from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import subprocess
from html import escape
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone

from apps.notes.mathtypeset_js import math_engine_js
from apps.notes.models import GeneratedNote, NoteImage, NotePdf
from apps.notes.renderers import SemanticNoteRenderer, mark_text_formula_boxes
from apps.notes.template_engine import NoteTemplateEngine
from apps.notes.themes import normalize_note_theme
from apps.notes.validators import NoteTemplateValidationError, validate_body_fragment
from apps.storage_lifecycle.paths import temporary_artifact_path
from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from infrastructure.queue.control import provider_concurrency_slot
from infrastructure.storage.base import StorageNotFoundError, StorageProviderError
from infrastructure.storage.factory import get_storage_service
from services.errors.recorder import ErrorRecordingService
from services.lectures.processing import LectureProcessingStateService

logger = logging.getLogger(__name__)


class NoteEditConflictError(ValueError):
    """Raised when a user attempts to overwrite a newer saved revision."""


class NoteImageValidationError(ValueError):
    """Raised when an uploaded file cannot be used as a note image."""


class NoteImageQuotaExceededError(ValueError):
    """Raised when the user's persistent storage quota blocks a new image."""

    def __init__(self, snapshot) -> None:
        self.snapshot = snapshot
        super().__init__("Persistent storage quota exceeded.")


class _EditableNoteBodySanitizer(HTMLParser):
    """Preserve the note template's safe markup and discard editable HTML hazards."""

    allowed_tags = frozenset(
        {
            "aside", "b", "br", "code", "div", "em", "figcaption", "figure", "footer", "h1", "h2",
            "h3", "h4", "header", "i", "img", "li", "main", "ol", "p", "pre", "section", "span", "strong",
            "sub", "sup", "table", "tbody", "td", "th", "thead", "tr", "ul",
        }
    )
    void_tags = frozenset({"br", "img"})
    # v5 diagrams: inline SVG is accepted only inside figure.v5-diagram, with these tags and the
    # strictly validated attributes below. No style, href, script, foreignObject or event handlers.
    svg_tags = frozenset(
        {"svg", "g", "rect", "circle", "ellipse", "line", "path", "polygon", "polyline", "text", "tspan", "defs", "marker"}
    )
    _SVG_NUMBER = re.compile(r"^-?\d{1,6}(?:\.\d{1,4})?%?$")
    _SVG_NUMERIC_ATTRS = frozenset(
        {"x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "refx", "refy",
         "markerwidth", "markerheight", "font-size"}
    )
    _SVG_VIEWBOX = re.compile(r"^-?\d{1,6}(?:\.\d{1,4})?(?: -?\d{1,6}(?:\.\d{1,4})?){3}$")
    _SVG_PATH_DATA = re.compile(r"^[0-9eE+\-., MmLlHhVvAaZzCcSsQqTt]{1,4000}$")
    _SVG_CLASS = re.compile(r"^(?:d|v5)-[a-z0-9_-]+$")
    _SVG_ENUMS = {
        "text-anchor": frozenset({"start", "middle", "end"}),
        "dominant-baseline": frozenset({"central", "middle", "auto", "hanging"}),
        "direction": frozenset({"rtl", "ltr"}),
        "orient": frozenset({"auto", "auto-start-reverse"}),
        "font-weight": frozenset({"normal", "bold", "100", "200", "300", "400", "500", "600", "700", "800", "900"}),
        "role": frozenset({"img"}),
        "marker-start": frozenset({"url(#ah)"}),
        "marker-end": frozenset({"url(#ah)"}),
        "id": frozenset({"ah"}),
        "xmlns": frozenset({"http://www.w3.org/2000/svg"}),
    }
    simple_attributes = frozenset(
        {"class", "dir", "role", "title", "alt", "colspan", "rowspan", "width", "height"}
    )
    # A narrow allowance for the crop geometry the editor's image-crop
    # handles write on the figure/img pair -- only plain pixel offsets and
    # the two layout keywords needed to make one element clip another, no
    # url()/expression()/selectors/anything else a style attribute could
    # otherwise carry.
    _STYLE_DECLARATION_PATTERN = re.compile(
        r"^(?:"
        r"(?:width|height|left|top|margin-left|margin-top):-?\d{1,5}px"
        r"|position:(?:relative|absolute)"
        r"|overflow:hidden"
        r")(?:;(?:"
        r"(?:width|height|left|top|margin-left|margin-top):-?\d{1,5}px"
        r"|position:(?:relative|absolute)"
        r"|overflow:hidden"
        r"))*;?$"
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self._in_diagram = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool = False) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in self.svg_tags:
            if not self._in_diagram:
                return
            cleaned_attrs = self._clean_svg_attributes(attrs)
            attributes = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in cleaned_attrs)
            self.parts.append(f"<{normalized_tag}{attributes}{'/' if self_closing else ''}>")
            return
        if normalized_tag not in self.allowed_tags:
            return
        cleaned_attrs = self._clean_attributes(normalized_tag, attrs)
        if normalized_tag == "figure" and any(
            name == "class" and "v5-diagram" in value.split() for name, value in cleaned_attrs
        ):
            self._in_diagram = True
        attributes = "".join(f' {name}="{escape(value, quote=True)}"' for name, value in cleaned_attrs)
        self.parts.append(f"<{normalized_tag}{attributes}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs, self_closing=tag.lower() in self.svg_tags)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.lower()
        if normalized_tag in self.svg_tags:
            if self._in_diagram:
                self.parts.append(f"</{normalized_tag}>")
            return
        if normalized_tag == "figure":
            self._in_diagram = False
        if normalized_tag in self.allowed_tags and normalized_tag not in self.void_tags:
            self.parts.append(f"</{normalized_tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(escape(data))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def _clean_attributes(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        cleaned: list[tuple[str, str]] = []
        for name, value in attrs:
            normalized_name = str(name or "").lower()
            normalized_value = str(value or "")
            if normalized_name in {"width", "height"} and not normalized_value.isdigit():
                continue
            if normalized_name in self.simple_attributes:
                cleaned.append((normalized_name, normalized_value))
            elif normalized_name.startswith("data-") or normalized_name.startswith("aria-"):
                cleaned.append((normalized_name, normalized_value))
            elif tag == "img" and normalized_name == "src" and self._safe_image_source(normalized_value):
                cleaned.append((normalized_name, normalized_value))
            elif (
                tag in {"figure", "img"}
                and normalized_name == "style"
                and self._safe_crop_style(normalized_value)
            ):
                cleaned.append((normalized_name, normalized_value))
            elif tag == "figure" and normalized_name == "contenteditable" and normalized_value == "false":
                cleaned.append((normalized_name, normalized_value))
        return cleaned

    @classmethod
    def _clean_svg_attributes(cls, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        cleaned: list[tuple[str, str]] = []
        for name, value in attrs:
            attr = str(name or "").lower()
            text = str(value or "").strip()
            if attr == "class":
                classes = [c for c in text.split() if cls._SVG_CLASS.match(c)]
                if classes:
                    cleaned.append((attr, " ".join(classes)))
            elif attr in cls._SVG_NUMERIC_ATTRS:
                if cls._SVG_NUMBER.match(text):
                    cleaned.append((attr, text))
            elif attr == "viewbox":
                if cls._SVG_VIEWBOX.match(text):
                    cleaned.append((attr, text))
            elif attr in {"d", "points"}:
                if cls._SVG_PATH_DATA.match(text):
                    cleaned.append((attr, text))
            elif attr in cls._SVG_ENUMS and text in cls._SVG_ENUMS[attr]:
                cleaned.append((attr, text))
        return cleaned

    @classmethod
    def _safe_crop_style(cls, value: str) -> bool:
        return bool(cls._STYLE_DECLARATION_PATTERN.match(value.strip()))

    @staticmethod
    def _safe_image_source(value: str) -> bool:
        if value == "{{THEME_LOGO_SRC}}" or value.startswith("data:image/"):
            return True
        note_image_prefix = f"{settings.BACKEND_PUBLIC_BASE_URL.rstrip('/')}/api/v1/notes/"
        return value.startswith(note_image_prefix) and "/images/" in value

    def rendered_html(self) -> str:
        return "".join(self.parts).strip()


class GeneratedNoteService:
    # Bump this when the PDF pipeline changes in a way that cannot be described
    # by the rendering fingerprint. Version 3 also invalidates the older PDFs
    # produced before summary-box consolidation was enforced end-to-end.
    PDF_ARTIFACT_VERSION = 3

    @staticmethod
    def _rendering_fingerprint(note: GeneratedNote) -> str:
        """Return a stable signature of the source that determines PDF output."""
        metadata = note.generation_metadata or {}
        language_code = str(metadata.get("output_language") or note.lecture.output_language or "fa")
        direction = str(metadata.get("output_direction") or "rtl")
        theme = normalize_note_theme(note.theme or note.lecture.processing_metadata.get("note_theme"))
        template = NoteTemplateEngine().load_template(language_code=language_code)
        renderer_script = (Path(settings.BASE_DIR) / "scripts" / "render_note_pdf.mjs").read_text(encoding="utf-8")

        # The browser template and renderer both participate in summary merging
        # and final-page validation. Hashing their source prevents a corrected
        # renderer from continuing to serve an older cached PDF artifact.
        source = "\x1f".join(
            (
                str(note.body_html or ""),
                language_code,
                direction,
                theme,
                template,
                renderer_script,
            )
        )
        return hashlib.sha256(source.encode("utf-8")).hexdigest()

    @staticmethod
    def _refresh_body_from_semantic_payload(note: GeneratedNote) -> bool:
        if bool((note.generation_metadata or {}).get("user_edited")):
            return False
        semantic_payload = note.semantic_payload if isinstance(note.semantic_payload, dict) else {}
        pages = semantic_payload.get("pages")
        if not isinstance(pages, list) or not pages:
            return False

        language_code = str(
            (note.generation_metadata or {}).get("output_language") or note.lecture.output_language or "fa"
        )
        rendered_body = GeneratedNoteService.render_body_from_semantic_document(
            document=semantic_payload,
            language_code=language_code,
        )
        if rendered_body.strip() == str(note.body_html or "").strip():
            return False

        # Keep the last successful artifacts available until their replacements
        # have been rendered and stored successfully.
        note.body_html = rendered_body
        note.save(update_fields=["body_html", "updated_at"])
        return True

    @staticmethod
    def list_notes(*, user):
        return GeneratedNote.objects.select_related("lecture", "pdf_record").filter(user=user)

    @staticmethod
    def get_note(*, user, note_id):
        return get_object_or_404(
            GeneratedNote.objects.select_related("lecture", "pdf_record"),
            id=note_id,
            user=user,
        )

    @staticmethod
    def render_full_html_from_body(
        *,
        body_content: str,
        minify: bool = False,
        theme: str = "green",
        language_code: str = "fa",
        direction: str = "rtl",
        for_editor: bool = False,
    ) -> str:
        return NoteTemplateEngine().render_with_options(
            body_content=mark_text_formula_boxes(validate_body_fragment(body_content)),
            minify=minify,
            theme=theme,
            language_code=language_code,
            direction=direction,
            for_editor=for_editor,
        )

    @staticmethod
    def render_body_from_semantic_document(*, document: dict[str, object], language_code: str = "fa") -> str:
        return SemanticNoteRenderer(language_code=language_code).render_document(document=document)

    @staticmethod
    def render_full_html_from_semantic_document(
        *,
        document: dict[str, object],
        minify: bool = False,
        theme: str = "green",
        language_code: str = "fa",
        direction: str = "rtl",
    ) -> str:
        body_content = GeneratedNoteService.render_body_from_semantic_document(
            document=document,
            language_code=language_code,
        )
        return NoteTemplateEngine().render_with_options(
            body_content=body_content,
            minify=minify,
            theme=theme,
            language_code=language_code,
            direction=direction,
        )

    @staticmethod
    def render_editor_html(*, note: GeneratedNote) -> str:
        language_code = str((note.generation_metadata or {}).get("output_language") or note.lecture.output_language or "fa")
        direction = str((note.generation_metadata or {}).get("output_direction") or "rtl")
        full_html = GeneratedNoteService.render_full_html_from_body(
            body_content=note.body_html,
            theme=normalize_note_theme(note.theme or note.lecture.processing_metadata.get("note_theme")),
            language_code=language_code,
            direction=direction,
            for_editor=True,
        )
        full_html = GeneratedNoteService._inline_note_images_for_editor(note=note, html=full_html)
        editor_canvas_style = """
<style id="tesanote-editor-canvas">
@media screen {
  html, body { width: 100%; min-width: 0; }
  body {
    display: block !important;
    min-height: 100% !important;
    padding: 0 !important;
    overflow-x: hidden;
  }
  .template-wrapper {
    width: 100% !important;
    max-width: none !important;
    gap: 18px !important;
  }
  .a4-page {
    box-sizing: border-box;
    width: 100% !important;
    max-width: none !important;
    margin: 0 !important;
  }
  .page-footer__page-number {
    display: none !important;
  }
}
</style>
"""
        editor_html = full_html.replace("</head>", f"{editor_canvas_style}</head>", 1)
        return editor_html.replace("</body>", f"{GeneratedNoteService._editor_bridge_script()}</body>", 1)

    _NOTE_IMAGE_SRC_PATTERN = re.compile(
        r'src="([^"]*?/api/v1/notes/[^"]+/images/([0-9a-fA-F-]{36})/)"'
    )

    @staticmethod
    def _inline_note_images_for_editor(*, note: GeneratedNote, html: str) -> str:
        """Rewrite note-image <img> tags to inline data: URIs for the editor.

        The editor iframe is rendered with a strict sandbox (no
        allow-same-origin), which gives it an opaque origin and blocks it
        from fetching anything over the network -- only data: URIs render.
        The saved body_html keeps the real HTTP URL; the bridge script's
        serializeBody() swaps it back in before every save (see the
        data-note-image-src attribute added here).
        """
        image_ids = {
            match.group(2) for match in GeneratedNoteService._NOTE_IMAGE_SRC_PATTERN.finditer(html)
        }
        if not image_ids:
            return html
        found = NoteImage.objects.filter(id__in=image_ids, note=note)
        images = {str(image.id): image for image in found}
        if not images:
            return html
        storage = get_storage_service()
        data_uri_cache: dict[str, str] = {}

        def _replace(match: re.Match[str]) -> str:
            original_src, image_id = match.group(1), match.group(2)
            image = images.get(image_id)
            if image is None:
                return match.group(0)
            if image_id not in data_uri_cache:
                try:
                    with storage.open(image.storage_path, "rb") as stream:
                        encoded = base64.b64encode(stream.read()).decode("ascii")
                except (StorageNotFoundError, StorageProviderError, OSError):
                    logger.warning(
                        "note_editor_image_inline_unavailable",
                        extra={"note_id": str(note.id), "image_id": image_id},
                        exc_info=True,
                    )
                    return match.group(0)
                data_uri_cache[image_id] = f"data:{image.content_type};base64,{encoded}"
            data_uri = data_uri_cache[image_id]
            return f'src="{data_uri}" data-note-image-src="{escape(original_src, quote=True)}"'

        return GeneratedNoteService._NOTE_IMAGE_SRC_PATTERN.sub(_replace, html)

    @staticmethod
    def _editor_bridge_script() -> str:
        """Add safe in-frame editing controls without changing the PDF template."""
        return """
<script id="tesanote-note-editor-bridge">
(() => {
  const SOURCE = 'tesanote-note-editor';
  const EDITABLE_SELECTOR = [
    '.page-header__course-name',
    '.page-header__course-detail',
    '.page-header__lecture-badge',
    '.page-content .chapter-header__title',
    '.page-content .chapter-header__subtitle',
    '.page-content .section-header__title',
    '.page-content .subsection-header',
    '.page-content .paragraph',
    '.page-content .key-highlight',
    '.page-content .formula-box__query-text',
    '.page-content .code-block__caption',
    '.page-content .code-block__code',
    '.page-content .list-block__title',
    '.page-content .list-block__item',
    '.page-content .example-box__title',
    '.page-content .note-box__title',
    '.page-content .summary-box__title',
    '.page-content .summary-box__list-item',
    '.page-content .exam-point__title',
    '.page-content .exam-point__content',
    '.page-content .unrelated-discussion-box__title',
    '.page-content .unrelated-discussion-box__content',
    '.page-content .figure-reference__caption',
    '.page-content .table-container__caption',
    '.page-content td',
    '.page-content th',
    '.v5-head h1',
    '.v5-head .v5-crumb',
    '.v5-head .v5-sub',
    '.v5-head .v5-lead',
    '.v5-head .v5-chip',
    '.page-content .v5-h2',
    '.page-content .v5-h3',
    '.page-content .v5-h4',
    '.page-content .v5-p',
    '.page-content .v5-list > li',
    '.page-content .v5-card h4',
    '.page-content .v5-card p',
    '.page-content .v5-card li',
    '.page-content .v5-q__q',
    '.page-content .v5-q__a',
    '.page-content .example-box__content .paragraph',
    '.page-content .example-box__content li',
    '.page-content .note-box__content .paragraph',
    '.page-content .note-box__content li'
  ].join(',');
  let selectedBlock = null;
  const undoStack = [];
  let structuralEditIsLatest = false;
  let isComposing = false;
  document.addEventListener('compositionstart', () => { isComposing = true; });
  document.addEventListener('compositionend', () => { isComposing = false; });

  function pushUndo() {
    const wrapper = document.querySelector('.template-wrapper');
    if (!wrapper) return;
    const html = wrapper.innerHTML;
    if (undoStack[undoStack.length - 1] === html) return;
    undoStack.push(html);
    structuralEditIsLatest = true;
    if (undoStack.length > 50) undoStack.shift();
  }

  function undoStructural() {
    const wrapper = document.querySelector('.template-wrapper');
    const html = undoStack.pop();
    if (!wrapper || html === undefined) return false;
    wrapper.innerHTML = html;
    selectBlock(null);
    markEditable();
    initializeImageCropFrames();
    reflowSoon();
    post('changed');
    return true;
  }
  let savedTextRange = null;
  let reflowTimer = null;
  let activeHighlightMode = null;
  let highlightSelectionTimer = null;

  const HIGHLIGHT_COLORS = {
    '#fde68a': 'tesanote-highlight--yellow',
    '#bbf7d0': 'tesanote-highlight--green',
    '#bae6fd': 'tesanote-highlight--blue',
    '#fecdd3': 'tesanote-highlight--pink'
  };

  const post = (action, payload = {}) => {
    window.parent.postMessage({ source: SOURCE, action, ...payload }, '*');
  };

  const blockTypeFor = (block) => block?.dataset?.blockType || 'paragraph';
  /*__TESANOTE_MATH_ENGINE__*/
  const LATEX_ESCAPE = String.fromCharCode(92);

  function selectBlock(block) {
    document.querySelectorAll('.tesanote-editor-selected').forEach((node) => {
      node.classList.remove('tesanote-editor-selected');
    });
    selectedBlock = block || null;
    if (selectedBlock) selectedBlock.classList.add('tesanote-editor-selected');
    post('selection', { selected: Boolean(selectedBlock), blockType: blockTypeFor(selectedBlock) });
  }

  function markEditable() {
    enhanceFormulaBoxes();
    document.querySelectorAll(EDITABLE_SELECTOR).forEach((node) => {
      node.setAttribute('contenteditable', 'true');
      node.setAttribute('spellcheck', 'true');
    });
  }

  function cleanFormulaSource(value) {
    let source = String(value || '')
      .replaceAll(String.fromCharCode(13), ' ')
      .replaceAll(String.fromCharCode(10), ' ')
      .trim();
    const inlineOpen = `${LATEX_ESCAPE}(`;
    const inlineClose = `${LATEX_ESCAPE})`;
    const displayOpen = `${LATEX_ESCAPE}[`;
    const displayClose = `${LATEX_ESCAPE}]`;
    if (source.startsWith(inlineOpen) && source.endsWith(inlineClose)) {
      source = source.slice(inlineOpen.length, -inlineClose.length);
    }
    if (source.startsWith(displayOpen) && source.endsWith(displayClose)) {
      source = source.slice(displayOpen.length, -displayClose.length);
    }
    while (source.startsWith('$')) source = source.slice(1);
    while (source.endsWith('$')) source = source.slice(0, -1);
    return source.trim();
  }

  function formulaTextNodes(box) {
    return Array.from(box.childNodes).filter((node) => node.nodeType === Node.TEXT_NODE);
  }

  function renderFormula(box, source) {
    const normalizedSource = cleanFormulaSource(source);
    const label = box.querySelector(':scope > .formula-box__label');
    formulaTextNodes(box).forEach((node) => node.remove());
    box.querySelectorAll(':scope > .formula-box__math, :scope > .formula-box__mixed').forEach((node) => node.remove());
    box.classList.remove('formula-box--mixed');
    const math = document.createElement('span');
    math.className = 'formula-box__math';
    math.setAttribute('dir', 'ltr');
    // Read-only until a plain click enters the source editor, so a drag can
    // select (and highlight) the typeset formula instead of replacing it.
    math.setAttribute('contenteditable', 'false');
    math.setAttribute('tabindex', '0');
    math.setAttribute('spellcheck', 'false');
    math.setAttribute('aria-label', normalizedSource || 'Empty formula');
    if (normalizedSource) {
      math.innerHTML = typesetMath(normalizedSource);
    } else {
      math.classList.add('formula-box__math--empty');
      math.textContent = 'فرمول را اینجا بنویسید';
    }
    box.dataset.formulaSource = normalizedSource;
    box.insertBefore(math, label || null);
  }

  function enhanceFormulaBoxes() {
    document.querySelectorAll('.page-content .formula-box:not(.formula-box--query)').forEach((box) => {
      // A formula that mixes Persian words and math is laid out by the server (words on a right-to-left
      // row, each math part on its own island). Rebuilding it here would show an empty formula.
      if (box.classList.contains('formula-box--mixed')) return;
      const rendered = box.querySelector(':scope > .formula-box__math');
      if (rendered) {
        // Server-rendered formulas arrive without editing hooks; make them editable like client-built ones.
        if (!rendered.classList.contains('formula-box__math--editing')) {
          rendered.setAttribute('contenteditable', 'false');
          rendered.setAttribute('tabindex', '0');
        }
        rendered.setAttribute('spellcheck', 'false');
        if (!box.dataset.formulaSource) {
          box.dataset.formulaSource = cleanFormulaSource(rendered.getAttribute('aria-label') || '');
        }
        return;
      }
      const source = box.dataset.formulaSource || formulaTextNodes(box).map((node) => node.textContent).join(' ').trim();
      // Prose in Persian/Arabic is text, not math: keep it as text, in the note's own face.
      if (!box.dataset.formulaSource && /[\\u0600-\\u06FF]/.test(source)) {
        box.classList.add('formula-box--text');
      }
      if (box.classList.contains('formula-box--text')) return;
      renderFormula(box, source);
    });
  }

  function reflowSoon() {
    window.clearTimeout(reflowTimer);
    reflowTimer = window.setTimeout(() => {
      window.__tesanotePreparePagination?.({ force: true });
    }, 650);
  }

  function rangeIsInNote(range) {
    const content = document.querySelector('.page-content');
    return Boolean(content && range && content.contains(range.commonAncestorContainer));
  }

  function rememberTextSelection() {
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) return false;
    const range = selection.getRangeAt(0);
    if (!rangeIsInNote(range)) return false;
    savedTextRange = range.cloneRange();
    return true;
  }

  function restoreTextSelection() {
    if (!savedTextRange || !rangeIsInNote(savedTextRange)) return false;
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(savedTextRange);
    return true;
  }

  function highlightClassFor(value) {
    const color = String(value || '').replaceAll(' ', '').toLowerCase();
    if (HIGHLIGHT_COLORS[color]) return HIGHLIGHT_COLORS[color];
    const rgb = color.match(/^rgb\\((\\d+),(\\d+),(\\d+)\\)$/);
    if (!rgb) return '';
    return HIGHLIGHT_COLORS[`#${Number(rgb[1]).toString(16).padStart(2, '0')}${Number(rgb[2]).toString(16).padStart(2, '0')}${Number(rgb[3]).toString(16).padStart(2, '0')}`] || '';
  }

  function normalizeHighlightMarkup() {
    document.querySelectorAll('.page-content [style*="background"], .page-content font[style*="background"]').forEach((node) => {
      const background = node.style.backgroundColor || node.style.background || '';
      const highlightClass = highlightClassFor(background);
      if (!highlightClass) return;
      let target = node;
      if (node.tagName === 'FONT') {
        target = document.createElement('span');
        while (node.firstChild) target.appendChild(node.firstChild);
        node.replaceWith(target);
      }
      target.removeAttribute('style');
      target.classList.add('tesanote-highlight', highlightClass);
    });
  }

  // Typeset formulas, including mixed Persian-and-math rows, are read-only surfaces.
  const FORMULA_SURFACE = '.formula-box__math:not(.formula-box__math--editing), .formula-box--mixed > .formula-box__mixed';

  function formulaMathOf(node) {
    const element = node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement;
    return element?.closest?.(FORMULA_SURFACE) || null;
  }

  // Typeset formulas are not contenteditable, so execCommand skips them; their
  // text pieces are wrapped by hand, like the editor's own highlight spans.
  function formulaTextSegments(range) {
    const segments = [];
    const seen = new Set();
    document.querySelectorAll('.page-content .formula-box__math:not(.formula-box__math--editing), .page-content .formula-box--mixed > .formula-box__mixed').forEach((math) => {
      if (seen.has(math) || !range.intersectsNode(math)) return;
      seen.add(math);
      const walker = document.createTreeWalker(math, NodeFilter.SHOW_TEXT);
      let node = walker.nextNode();
      while (node) {
        if (range.intersectsNode(node)) {
          const start = node === range.startContainer ? range.startOffset : 0;
          const end = node === range.endContainer ? range.endOffset : node.length;
          if (end > start && node.data.slice(start, end).trim()) segments.push({ node, start, end });
        }
        node = walker.nextNode();
      }
    });
    return segments;
  }

  function highlightFormulaSegments(segments, highlightClass) {
    segments.forEach(({ node, start, end }) => {
      if (!node.isConnected) return;
      const existing = node.parentElement?.closest('.tesanote-highlight');
      if (existing && formulaMathOf(existing)) {
        Object.values(HIGHLIGHT_COLORS).forEach((name) => existing.classList.remove(name));
        existing.classList.add(highlightClass);
        return;
      }
      const part = document.createRange();
      part.setStart(node, start);
      part.setEnd(node, end);
      const span = document.createElement('span');
      span.className = 'tesanote-highlight ' + highlightClass;
      part.surroundContents(span);
    });
  }

  function enterFormulaEdit(math) {
    if (!math || math.classList.contains('formula-box__math--editing')) return;
    const formulaBox = math.closest('.formula-box:not(.formula-box--query)');
    if (!formulaBox) return;
    math.setAttribute('contenteditable', 'true');
    math.classList.remove('formula-box__math--empty');
    math.classList.add('formula-box__math--editing');
    math.textContent = formulaBox.dataset.formulaSource || '';
    math.focus();
  }

  function applyHighlight(color, range = savedTextRange) {
    if (!range) return false;
    pushUndo();
    savedTextRange = range.cloneRange();
    if (!restoreTextSelection()) return false;
    const highlightClass = highlightClassFor(color);
    const formulaSegments = highlightClass ? formulaTextSegments(range) : [];
    const startMath = formulaMathOf(range.startContainer);
    const insideOneFormula = Boolean(startMath) && startMath === formulaMathOf(range.endContainer);
    if (!insideOneFormula) document.execCommand('hiliteColor', false, color);
    highlightFormulaSegments(formulaSegments, highlightClass);
    normalizeHighlightMarkup();
    reflowSoon();
    post('changed');
    return true;
  }

  function rangeContainsNode(range, node) {
    const nodeRange = document.createRange();
    nodeRange.selectNode(node);
    return (
      range.compareBoundaryPoints(Range.START_TO_START, nodeRange) <= 0 &&
      range.compareBoundaryPoints(Range.END_TO_END, nodeRange) >= 0
    );
  }

  function unwrapHighlight(node) {
    const parent = node.parentNode;
    if (!parent) return;
    while (node.firstChild) parent.insertBefore(node.firstChild, node);
    node.remove();
  }

  function textOffsetInNode(node, container, offset) {
    const before = document.createRange();
    before.selectNodeContents(node);
    before.setEnd(container, offset);
    return before.toString().length;
  }

  function splitHighlightAt(node, offset) {
    const length = node.textContent?.length || 0;
    if (offset <= 0) return node;
    if (offset >= length) return node.nextSibling;
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let remaining = offset;
    let textNode = walker.nextNode();
    while (textNode) {
      const textLength = textNode.textContent?.length || 0;
      if (remaining <= textLength) {
        const range = document.createRange();
        range.setStart(textNode, remaining);
        range.setEnd(node, node.childNodes.length);
        const tail = range.extractContents();
        const split = node.cloneNode(false);
        split.appendChild(tail);
        node.after(split);
        return split;
      }
      remaining -= textLength;
      textNode = walker.nextNode();
    }
    return node.nextSibling;
  }

  function eraseHighlight(range = savedTextRange) {
    if (!range) return false;
    pushUndo();
    const targets = Array.from(document.querySelectorAll('.page-content .tesanote-highlight'))
      .filter((node) => range.intersectsNode(node));
    targets.forEach((node) => {
      if (!node.isConnected) return;
      const nodeRange = document.createRange();
      nodeRange.selectNodeContents(node);
      const length = node.textContent?.length || 0;
      const start = range.compareBoundaryPoints(Range.START_TO_START, nodeRange) <= 0
        ? 0
        : textOffsetInNode(node, range.startContainer, range.startOffset);
      const end = range.compareBoundaryPoints(Range.END_TO_END, nodeRange) >= 0
        ? length
        : textOffsetInNode(node, range.endContainer, range.endOffset);
      if (end <= start) return;
      if (end < length) splitHighlightAt(node, end);
      const selectedPart = start > 0 ? splitHighlightAt(node, start) : node;
      if (selectedPart?.classList?.contains('tesanote-highlight')) unwrapHighlight(selectedPart);
    });
    document.querySelectorAll('.page-content .tesanote-highlight').forEach((node) => {
      if (!node.textContent?.trim()) node.remove();
    });
    savedTextRange = null;
    window.getSelection()?.removeAllRanges();
    reflowSoon();
    post('changed');
    return true;
  }

  function applyActiveHighlight() {
    if (!activeHighlightMode || !rememberTextSelection()) return;
    const range = savedTextRange?.cloneRange();
    if (!range) return;
    if (activeHighlightMode === 'erase') eraseHighlight(range);
    else applyHighlight(activeHighlightMode, range);
  }

  function scheduleActiveHighlight() {
    if (!activeHighlightMode) return;
    window.clearTimeout(highlightSelectionTimer);
    highlightSelectionTimer = window.setTimeout(applyActiveHighlight, 0);
  }

  function editorBlock(type) {
    const block = document.createElement('div');
    block.className = 'note-block';
    block.dataset.blockType = type;
    const content = {
      paragraph: '<p class="paragraph">متن این بخش را اینجا بنویسید.</p>',
      'note-box': '<div class="note-box"><div class="note-box__header"><span class="note-box__icon"><span class="note-box__icon-mark">!</span></span><span class="note-box__title">نکته</span></div><div class="note-box__content"><p class="paragraph paragraph--compact">متن نکته را اینجا بنویسید.</p></div></div>',
      'example-box': '<div class="example-box"><div class="example-box__header"><span class="example-box__icon"><span class="example-box__icon-inner"></span></span><span class="example-box__title">مثال</span></div><div class="example-box__content"><p class="paragraph paragraph--compact">صورت مثال را اینجا بنویسید.</p><span class="example-box__solution-label">▼ راه‌حل:</span><p class="paragraph paragraph--compact u-mt-sm">راه‌حل مثال را اینجا بنویسید.</p></div></div>',
      'summary-box': '<div class="summary-box"><div class="summary-box__header"><span class="summary-box__icon">📋</span><span class="summary-box__title">جمع‌بندی</span></div><div class="summary-box__content"><ul class="summary-box__list"><li class="summary-box__list-item">نکته جمع‌بندی را اینجا بنویسید.</li></ul></div></div>',
      'exam-point': '<div class="exam-point"><div class="exam-point__header"><span class="exam-point__icon">🎯</span><span class="exam-point__title">نکته امتحانی</span></div><div class="exam-point__content">متن نکته امتحانی را اینجا بنویسید.</div></div>',
      'formula-box': '<div class="formula-box" role="math" data-formula-source="x = y"></div>'
    };
    block.innerHTML = content[type] || content.paragraph;
    return block;
  }

  function insertBlock(type) {
    pushUndo();
    // Summary content is intentionally kept in one final box. Adding a
    // summary item extends that box instead of recreating the historical
    // duplicate-summary PDF bug from the editor.
    if (type === 'summary-box') {
      const existingSummary = document.querySelector('.page-content .summary-box');
      if (existingSummary) {
        const list = existingSummary.querySelector('.summary-box__list');
        if (list) {
          const item = document.createElement('li');
          item.className = 'summary-box__list-item';
          item.textContent = 'نکته جمع‌بندی را اینجا بنویسید.';
          list.appendChild(item);
          markEditable();
          selectBlock(existingSummary.closest('.note-block'));
          reflowSoon();
          post('changed');
          return;
        }
      }
    }
    placeBlockAtSelection(editorBlock(type));
  }

  function placeBlockAtSelection(block) {
    const targetPage = selectedBlock?.closest('.a4-page') || document.querySelector('.a4-page:last-of-type');
    const pageContent = targetPage?.querySelector('.page-content');
    if (!pageContent) return;
    if (selectedBlock?.parentElement === pageContent) {
      selectedBlock.insertAdjacentElement('afterend', block);
    } else {
      pageContent.appendChild(block);
    }
    markEditable();
    selectBlock(block);
    reflowSoon();
    post('changed');
  }

  // A cropped image is rendered at its own (possibly larger-than-frame)
  // pixel size, absolutely positioned inside a frame (the <figure>) that
  // clips it via overflow:hidden -- the standard "oversized image behind a
  // clipping window" technique. The frame's width/height is the visible
  // crop; the image's left/top (always <= 0) is how much of the image is
  // pushed out above/left of the frame, i.e. already cropped from that
  // side. This state lives entirely in each element's own inline style
  // (not JS-only state) so it survives save/reload and renders identically
  // in the read-only view and PDF export, neither of which runs this
  // script. The sanitizer that cleans a saved edit (_EditableNoteBodySanitizer
  // in services.py) allows only this narrow set of style declarations.
  function applyImageCropState(figure, img, state) {
    const frameW = Math.round(state.frameW);
    const frameH = Math.round(state.frameH);
    const imgW = Math.round(state.imgW);
    const imgH = Math.round(state.imgH);
    const left = Math.round(-state.offsetX);
    const top = Math.round(-state.offsetY);
    figure.setAttribute(
      'style',
      `width:${frameW}px;height:${frameH}px;overflow:hidden;position:relative`
    );
    img.setAttribute(
      'style',
      `position:absolute;width:${imgW}px;height:${imgH}px;left:${left}px;top:${top}px`
    );
    img.removeAttribute('width');
    img.removeAttribute('height');
  }

  function readImageCropState(block) {
    const figure = block?.querySelector('.note-image-figure');
    const img = figure?.querySelector('img');
    if (!figure || !img) return null;
    const frameW = parseFloat(figure.style.width);
    const frameH = parseFloat(figure.style.height);
    const imgW = parseFloat(img.style.width);
    const imgH = parseFloat(img.style.height);
    if (frameW > 0 && frameH > 0 && imgW > 0 && imgH > 0) {
      return {
        figure,
        img,
        frameW,
        frameH,
        imgW,
        imgH,
        offsetX: -(parseFloat(img.style.left) || 0),
        offsetY: -(parseFloat(img.style.top) || 0),
      };
    }
    // A legacy image saved before cropping existed: it only has plain
    // width/height attributes and no crop frame yet. Back-fill the frame
    // from its current rendered size (full image visible, nothing cropped)
    // so the new crop handles have a valid state to start dragging from.
    const fallbackW = img.width || img.getBoundingClientRect().width || 320;
    const fallbackH = img.height || img.getBoundingClientRect().height || 220;
    const state = {
      figure,
      img,
      frameW: fallbackW,
      frameH: fallbackH,
      imgW: fallbackW,
      imgH: fallbackH,
      offsetX: 0,
      offsetY: 0,
    };
    applyImageCropState(figure, img, state);
    return state;
  }

  function initializeImageCropFrames() {
    document.querySelectorAll('.note-image-block').forEach((block) => readImageCropState(block));
  }

  function insertImageBlock(payload) {
    pushUndo();
    const naturalWidth = Number(payload?.width) || 480;
    const naturalHeight = Number(payload?.height) || 320;
    const aspect = naturalWidth > 0 ? naturalHeight / naturalWidth : 0.66;
    const initialWidth = Math.max(160, Math.min(naturalWidth, 640));
    const initialHeight = initialWidth * aspect;

    const block = document.createElement('div');
    block.className = 'note-block note-image-block';
    block.dataset.blockType = 'image';
    block.dataset.aspect = String(aspect);

    const figure = document.createElement('figure');
    figure.className = 'note-image-figure';
    const img = document.createElement('img');
    // This sandboxed iframe cannot fetch over the network, so the freshly
    // uploaded image is shown via the data: URI the parent already has
    // client-side; the real URL is kept on data-note-image-src and swapped
    // back in by serializeBody() before the note is ever saved.
    img.src = String(payload?.dataUrl || payload?.url || '');
    if (payload?.url) img.dataset.noteImageSrc = String(payload.url);
    img.alt = '';
    figure.appendChild(img);
    applyImageCropState(figure, img, {
      frameW: initialWidth,
      frameH: initialHeight,
      imgW: initialWidth,
      imgH: initialHeight,
      offsetX: 0,
      offsetY: 0,
    });

    const controls = document.createElement('div');
    controls.className = 'note-image-block__controls';
    controls.setAttribute('contenteditable', 'false');
    const deleteButton = document.createElement('button');
    deleteButton.type = 'button';
    deleteButton.className = 'note-image-block__delete';
    deleteButton.title = 'حذف عکس';
    deleteButton.setAttribute('aria-label', 'حذف عکس');
    deleteButton.textContent = '×';
    const resizeHandle = document.createElement('span');
    resizeHandle.className = 'note-image-block__resize-handle';
    resizeHandle.title = 'برای تغییر اندازه بکشید و رها کنید';
    controls.append(deleteButton, resizeHandle);
    ['top', 'bottom', 'left', 'right'].forEach((side) => {
      const cropHandle = document.createElement('span');
      cropHandle.className = `note-image-block__crop-handle note-image-block__crop-handle--${side}`;
      cropHandle.dataset.cropSide = side;
      cropHandle.title = 'برای برش عکس از این لبه بکشید';
      controls.appendChild(cropHandle);
    });
    block.append(figure, controls);

    placeBlockAtSelection(block);
  }

  let resizingImageBlock = null;
  let resizeStartX = 0;
  let resizeStartState = null;

  function beginImageResize(event, block) {
    const state = readImageCropState(block);
    if (!state) return;
    resizingImageBlock = block;
    resizeStartX = event.clientX;
    resizeStartState = state;
    event.preventDefault();
    event.stopPropagation();
    selectBlock(block);
    document.body.classList.add('tesanote-resizing-image');
  }

  function performImageResize(event) {
    if (!resizingImageBlock || !resizeStartState) return;
    const pageContent = resizingImageBlock.closest('.page-content');
    const maxWidth = pageContent ? pageContent.clientWidth : 900;
    const delta = event.clientX - resizeStartX;
    const nextFrameW = Math.max(120, Math.min(maxWidth, resizeStartState.frameW + delta));
    const scale = nextFrameW / resizeStartState.frameW;
    applyImageCropState(resizeStartState.figure, resizeStartState.img, {
      frameW: nextFrameW,
      frameH: resizeStartState.frameH * scale,
      imgW: resizeStartState.imgW * scale,
      imgH: resizeStartState.imgH * scale,
      offsetX: resizeStartState.offsetX * scale,
      offsetY: resizeStartState.offsetY * scale,
    });
  }

  function endImageResize() {
    if (!resizingImageBlock) return;
    resizingImageBlock = null;
    resizeStartState = null;
    document.body.classList.remove('tesanote-resizing-image');
    reflowSoon();
    post('changed');
  }

  // Crop-from-one-edge: the handle on that edge is dragged inward, which
  // shrinks the frame from that side only -- the opposite edge, and
  // everything the image shows along it, does not move. Dragging a side
  // back outward (up to the image's full, uncropped extent) restores what
  // was cropped away; it can never reveal more than the original photo.
  let croppingImageBlock = null;
  let cropSide = null;
  let cropStartX = 0;
  let cropStartY = 0;
  let cropStartState = null;
  const MIN_CROP_SIZE = 40;

  function beginImageCrop(event, block, side) {
    const state = readImageCropState(block);
    if (!state) return;
    croppingImageBlock = block;
    cropSide = side;
    cropStartX = event.clientX;
    cropStartY = event.clientY;
    cropStartState = state;
    event.preventDefault();
    event.stopPropagation();
    selectBlock(block);
    document.body.classList.add('tesanote-cropping-image');
  }

  function performImageCrop(event) {
    if (!croppingImageBlock || !cropStartState) return;
    const { figure, img, frameW, frameH, imgW, imgH, offsetX, offsetY } = cropStartState;
    let nextFrameW = frameW;
    let nextFrameH = frameH;
    let nextOffsetX = offsetX;
    let nextOffsetY = offsetY;

    if (cropSide === 'bottom') {
      const dy = event.clientY - cropStartY;
      nextFrameH = frameH + dy;
    } else if (cropSide === 'top') {
      const dy = event.clientY - cropStartY;
      nextFrameH = frameH - dy;
      nextOffsetY = offsetY + dy;
    } else if (cropSide === 'right') {
      const dx = event.clientX - cropStartX;
      nextFrameW = frameW + dx;
    } else if (cropSide === 'left') {
      const dx = event.clientX - cropStartX;
      nextFrameW = frameW - dx;
      nextOffsetX = offsetX + dx;
    }

    nextFrameW = Math.max(MIN_CROP_SIZE, Math.min(imgW, nextFrameW));
    nextFrameH = Math.max(MIN_CROP_SIZE, Math.min(imgH, nextFrameH));
    nextOffsetX = Math.max(0, Math.min(imgW - nextFrameW, nextOffsetX));
    nextOffsetY = Math.max(0, Math.min(imgH - nextFrameH, nextOffsetY));

    applyImageCropState(figure, img, {
      frameW: nextFrameW,
      frameH: nextFrameH,
      imgW,
      imgH,
      offsetX: nextOffsetX,
      offsetY: nextOffsetY,
    });
  }

  function endImageCrop() {
    if (!croppingImageBlock) return;
    croppingImageBlock = null;
    cropSide = null;
    cropStartState = null;
    document.body.classList.remove('tesanote-cropping-image');
    reflowSoon();
    post('changed');
  }

  function deleteImageBlock(block) {
    const blocks = document.querySelectorAll('.page-content .note-block');
    if (blocks.length <= 1 || !block) return;
    const img = block.querySelector('.note-image-figure img');
    // src is a data: URI in this sandboxed iframe (see insertImageBlock);
    // the real URL -- which the id is parsed from -- lives on this attribute.
    const realSrc = img?.getAttribute('data-note-image-src') || img?.getAttribute('src') || '';
    const imageId = realSrc.split('/').filter(Boolean).pop();
    undoStack.length = 0;
    const next = block.nextElementSibling || block.previousElementSibling;
    block.remove();
    if (selectedBlock === block) selectBlock(next?.classList?.contains('note-block') ? next : null);
    if (imageId) post('delete-image', { imageId });
    reflowSoon();
    post('changed');
  }

  function focusFormulaMath(formulaBox) {
    const math = formulaBox?.querySelector?.('.formula-box__math');
    if (math) window.setTimeout(() => math.focus(), 0);
  }

  function openFormula() {
    const selectedFormula = selectedBlock?.querySelector?.('.formula-box:not(.formula-box--query)');
    if (selectedFormula) {
      focusFormulaMath(selectedFormula);
      return;
    }
    const block = editorBlock('formula-box');
    const formulaBox = block.querySelector('.formula-box');
    if (formulaBox) renderFormula(formulaBox, '');
    const targetPage = selectedBlock?.closest('.a4-page') || document.querySelector('.a4-page:last-of-type');
    const pageContent = targetPage?.querySelector('.page-content');
    if (!pageContent) return;
    if (selectedBlock?.parentElement === pageContent) selectedBlock.insertAdjacentElement('afterend', block);
    else pageContent.appendChild(block);
    markEditable();
    selectBlock(block);
    focusFormulaMath(formulaBox);
    reflowSoon();
    post('changed');
  }

  function insertFormula(formula, append) {
    pushUndo();
    const selectedFormula = selectedBlock?.querySelector?.('.formula-box:not(.formula-box--query)');
    if (selectedFormula) {
      const currentSource = cleanFormulaSource(selectedFormula.dataset.formulaSource);
      renderFormula(selectedFormula, append ? `${currentSource}${formula}` : formula);
      markEditable();
      focusFormulaMath(selectedFormula);
      reflowSoon();
      post('changed');
      return;
    }
    const block = editorBlock('formula-box');
    const formulaBox = block.querySelector('.formula-box');
    if (formulaBox) renderFormula(formulaBox, formula);
    const targetPage = selectedBlock?.closest('.a4-page') || document.querySelector('.a4-page:last-of-type');
    const pageContent = targetPage?.querySelector('.page-content');
    if (!pageContent) return;
    if (selectedBlock?.parentElement === pageContent) selectedBlock.insertAdjacentElement('afterend', block);
    else pageContent.appendChild(block);
    markEditable();
    selectBlock(block);
    focusFormulaMath(formulaBox);
    reflowSoon();
    post('changed');
  }

  function removeSelectedBlock() {
    if (!selectedBlock) return;
    pushUndo();
    const blocks = document.querySelectorAll('.page-content .note-block');
    if (blocks.length <= 1) return;
    const next = selectedBlock.nextElementSibling || selectedBlock.previousElementSibling;
    selectedBlock.remove();
    selectBlock(next?.classList?.contains('note-block') ? next : null);
    reflowSoon();
    post('changed');
  }

  function serializeBody(options) {
    const wrapper = document.querySelector('.template-wrapper');
    if (!wrapper) return '';
    // Autosave must not mutate the live DOM under the user's caret.
    if (!options || !options.preserveLiveDom) normalizeHighlightMarkup();
    const copy = wrapper.cloneNode(true);
    copy.querySelectorAll('[contenteditable], .tesanote-editor-selected').forEach((node) => {
      node.removeAttribute('contenteditable');
      node.removeAttribute('spellcheck');
      node.classList?.remove('tesanote-editor-selected');
    });
    copy.querySelectorAll('.formula-box__math[tabindex]').forEach((node) => node.removeAttribute('tabindex'));
    // The mobile fit-to-width zoom is display-only and would fail the style whitelist.
    copy.querySelectorAll('.note-image-figure').forEach((figure) => figure.style.removeProperty('zoom'));
    copy.querySelectorAll('.page-header__brand-logo').forEach((image) => {
      image.setAttribute('src', '{{THEME_LOGO_SRC}}');
    });
    // Note-image <img> tags are shown with an inline data: URI in this
    // sandboxed iframe (it cannot fetch over the network); swap each one
    // back to its real, stable URL before this leaves the iframe.
    copy.querySelectorAll('img[data-note-image-src]').forEach((image) => {
      image.setAttribute('src', image.getAttribute('data-note-image-src'));
      image.removeAttribute('data-note-image-src');
    });
    // Resize/delete controls are editor-only chrome, never part of the saved
    // document (they'd otherwise ship into the read-only view and the PDF).
    copy.querySelectorAll('.note-image-block__controls').forEach((node) => node.remove());
    // The accuracy-check overlay is a viewing aid, never part of the saved document.
    copy.querySelectorAll('.tesanote-review-strip').forEach((node) => node.remove());
    copy.querySelectorAll('.tesanote-review-flag').forEach((node) => node.classList.remove('tesanote-review-flag'));
    return copy.outerHTML;
  }

  // ---- Accuracy check overlay (set from the page; removed again before any save) ----
  function reviewHeadingKey(value) {
    return String(value || '').replace(/\\u200c/g, ' ').replace(/\\s+/g, ' ').trim();
  }

  let reviewObserver = null;

  function clearReviewMarkers() {
    if (reviewObserver) { reviewObserver.disconnect(); reviewObserver = null; }
    document.querySelectorAll('.tesanote-review-strip').forEach((node) => node.remove());
    document.querySelectorAll('.tesanote-review-flag').forEach((node) => node.classList.remove('tesanote-review-flag'));
  }

  // The blocks a section covers: from its header block up to the next header, across pages.
  function reviewSections() {
    const blocks = Array.from(document.querySelectorAll('.page-content .note-block'));
    const sections = [];
    blocks.forEach((block) => {
      const title = block.querySelector('.section-header__title');
      if (title) {
        sections.push({ key: reviewHeadingKey(title.textContent), header: block, body: [] });
      } else if (sections.length) {
        sections[sections.length - 1].body.push(block);
      }
    });
    return sections;
  }

  function applyReviewMarkers(spots, tag, hint) {
    clearReviewMarkers();
    const sections = reviewSections();
    const list = spots || [];
    // Spots from reviews made before positions were stored: spread them through their section in time order.
    const siblings = new Map();
    list.forEach((spot) => {
      const group = siblings.get(spot.sectionIndex) || [];
      group.push(spot);
      siblings.set(spot.sectionIndex, group);
    });
    const rows = new Map();
    list.forEach((spot) => {
      const wanted = reviewHeadingKey(spot.heading);
      const section = (wanted && sections.find((item) => item.key === wanted)) || sections[spot.sectionIndex];
      if (!section) return;
      let fraction = typeof spot.fraction === 'number' ? spot.fraction : null;
      if (fraction === null) {
        const group = siblings.get(spot.sectionIndex) || [spot];
        fraction = (group.indexOf(spot) + 0.5) / group.length;
      }
      const body = section.body.filter((block) => !block.classList.contains('note-image-block'));
      const target = body.length
        ? body[Math.min(body.length - 1, Math.max(0, Math.floor(fraction * body.length)))]
        : section.header;
      let row = rows.get(target);
      if (!row) {
        target.classList.add('tesanote-review-flag');
        row = document.createElement('div');
        row.className = 'tesanote-review-strip';
        row.setAttribute('contenteditable', 'false');
        row.setAttribute('aria-label', tag || '');
        const icon = document.createElement('span');
        icon.className = 'tesanote-review-icon';
        icon.setAttribute('aria-hidden', 'true');
        for (let bar = 0; bar < 4; bar += 1) icon.appendChild(document.createElement('i'));
        row.appendChild(icon);
        target.parentNode.insertBefore(row, target);
        rows.set(target, row);
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'tesanote-review-spot';
      button.setAttribute('data-spot-id', spot.id);
      button.setAttribute('aria-label', spot.ariaLabel || '');
      button.setAttribute('data-hint', hint || '');
      button.textContent = spot.timeLabel || '';
      // Direct listeners: the chip must work whatever the document-level handlers do.
      const open = (event) => {
        event.preventDefault();
        event.stopPropagation();
        post('review-spot-click', { spotId: spot.id });
      };
      button.addEventListener('click', open);
      button.addEventListener('pointerdown', (event) => event.stopPropagation());
      button.addEventListener('mousedown', (event) => event.stopPropagation());
      row.appendChild(button);
    });
    // Markers appear as the reader reaches them, not all at once.
    const strips = Array.from(rows.values());
    if ('IntersectionObserver' in window) {
      reviewObserver = new IntersectionObserver((entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          entry.target.classList.add('is-in');
          reviewObserver && reviewObserver.unobserve(entry.target);
        });
      }, { rootMargin: '0px 0px 160px 0px' });
      strips.forEach((row) => reviewObserver.observe(row));
      // Whatever is already on screen shows at once, without waiting for the first observer pass.
      const reach = window.innerHeight + 160;
      strips.forEach((row) => {
        const box = row.getBoundingClientRect();
        if (box.top < reach && box.bottom > -160) row.classList.add('is-in');
      });
    } else {
      strips.forEach((row) => row.classList.add('is-in'));
    }
  }

  // ---- Floating text-format toolbar (bold / italic / color / size) ----
  const FMT_COLORS = [
    ['default', '', 'پیش‌فرض'], ['red', '#dc2626', 'قرمز'], ['orange', '#ea580c', 'نارنجی'],
    ['green', '#16a34a', 'سبز'], ['blue', '#2563eb', 'آبی'], ['purple', '#7c3aed', 'بنفش'],
    ['gray', '#64748b', 'خاکستری']
  ];
  const FMT_SIZES = [['sm', 'کوچک'], ['normal', 'معمولی'], ['lg', 'بزرگ'], ['xl', 'خیلی بزرگ']];
  const FMT_GROUPS = {
    bold: { match: (el) => el.tagName === 'STRONG' || el.tagName === 'B' },
    italic: { match: (el) => el.tagName === 'EM' || el.tagName === 'I' },
    color: { prefix: 'tesanote-fc--' },
    size: { prefix: 'tesanote-fs--' }
  };
  let fmtBar = null;
  let fmtPanel = null;
  let fmtPointerDown = false;
  let fmtFrame = 0;

  function fmtClassOf(el, group) {
    const prefix = FMT_GROUPS[group].prefix;
    return Array.from(el.classList || []).filter((name) => name.startsWith(prefix));
  }
  function isFmtElement(el) {
    if (!el || el.nodeType !== 1) return false;
    if (['STRONG', 'B', 'EM', 'I'].includes(el.tagName)) return true;
    return el.tagName === 'SPAN' && Array.from(el.classList).some((name) => name.startsWith('tesanote-fc--') || name.startsWith('tesanote-fs--'));
  }
  function fmtMatches(el, group) {
    const def = FMT_GROUPS[group];
    return def.match ? def.match(el) : fmtClassOf(el, group).length > 0;
  }
  function fmtChain(node, group) {
    const chain = [];
    let el = node.parentElement;
    while (el && isFmtElement(el)) {
      if (fmtMatches(el, group)) chain.push(el);
      el = el.parentElement;
    }
    return chain;
  }
  function fmtUnwrap(el) {
    const parent = el.parentNode;
    while (el.firstChild) parent.insertBefore(el.firstChild, el);
    el.remove();
  }
  function fmtClearGroup(el, group) {
    if (FMT_GROUPS[group].match) { fmtUnwrap(el); return; }
    fmtClassOf(el, group).forEach((name) => el.classList.remove(name));
    if (el.tagName === 'SPAN' && !isFmtElement(el)) fmtUnwrap(el);
  }
  function fmtIsolate(anc, node) {
    const before = document.createRange();
    before.setStart(anc, 0);
    before.setEndBefore(node);
    const after = document.createRange();
    after.setStartAfter(node);
    after.setEnd(anc, anc.childNodes.length);
    const tail = after.extractContents();
    const head = before.extractContents();
    if (tail.textContent) { const c = anc.cloneNode(false); c.appendChild(tail); anc.after(c); }
    if (head.textContent) { const c = anc.cloneNode(false); c.appendChild(head); anc.before(c); }
  }
  function fmtTextNodes(range) {
    const root = range.commonAncestorContainer.nodeType === 3 ? range.commonAncestorContainer.parentNode : range.commonAncestorContainer;
    const nodes = [];
    if (range.commonAncestorContainer.nodeType === 3) nodes.push(range.commonAncestorContainer);
    else {
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      let n = walker.nextNode();
      while (n) { if (range.intersectsNode(n)) nodes.push(n); n = walker.nextNode(); }
    }
    const content = document.querySelector('.page-content');
    return nodes.filter((n) => {
      if (!n.textContent.trim()) return false;
      if (!content || !content.contains(n)) return false;
      return !n.parentElement.closest('.formula-box__math, .note-image-block__controls, [contenteditable="false"]');
    });
  }
  function fmtSplitEnds(range) {
    let { startContainer: sc, startOffset: so, endContainer: ec, endOffset: eo } = range;
    if (ec.nodeType === 3 && eo < ec.length) ec.splitText(eo);
    if (sc.nodeType === 3 && so > 0) {
      const tail = sc.splitText(so);
      if (sc === ec) ec = tail;
      sc = tail;
    }
    const next = document.createRange();
    next.setStart(sc, sc.nodeType === 3 ? 0 : so);
    next.setEnd(ec, ec.nodeType === 3 ? ec.length : eo);
    return next;
  }

  function applyFormat(group, value) {
    if (!savedTextRange || !rangeIsInNote(savedTextRange)) return false;
    let range = fmtSplitEnds(savedTextRange.cloneRange());
    const nodes = fmtTextNodes(range);
    if (!nodes.length) return false;
    pushUndo();
    const def = FMT_GROUPS[group];
    let mode = 'set';
    if (def.match) mode = nodes.every((n) => fmtChain(n, group).length) ? 'remove' : 'set';
    else if (!value || value === 'default' || value === 'normal') mode = 'remove';
    nodes.forEach((n) => {
      fmtChain(n, group).slice().reverse().forEach((anc) => { if (anc.contains(n)) fmtIsolate(anc, n); });
      fmtChain(n, group).forEach((anc) => fmtClearGroup(anc, group));
      if (mode !== 'set') return;
      const parent = n.parentElement;
      if (!def.match && parent && parent.tagName === 'SPAN' && isFmtElement(parent) && parent.childNodes.length === 1) {
        parent.classList.add(def.prefix + value);
        return;
      }
      const wrap = document.createElement(def.match ? (group === 'bold' ? 'strong' : 'em') : 'span');
      if (!def.match) wrap.className = def.prefix + value;
      n.replaceWith(wrap);
      wrap.appendChild(n);
    });
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    const sel = window.getSelection();
    const restored = document.createRange();
    restored.setStart(first, 0);
    restored.setEnd(last, last.length);
    sel?.removeAllRanges();
    sel?.addRange(restored);
    savedTextRange = restored.cloneRange();
    reflowSoon();
    post('changed');
    syncFmtState();
    positionFmtBar();
    return true;
  }

  function fmtState() {
    const nodes = savedTextRange && rangeIsInNote(savedTextRange) ? fmtTextNodes(savedTextRange) : [];
    const state = { bold: false, italic: false, color: '', size: '' };
    if (!nodes.length) return state;
    state.bold = nodes.every((n) => fmtChain(n, 'bold').length);
    state.italic = nodes.every((n) => fmtChain(n, 'italic').length);
    const pick = (group) => {
      const values = new Set(nodes.map((n) => {
        const el = fmtChain(n, group)[0];
        return el ? fmtClassOf(el, group)[0].slice(FMT_GROUPS[group].prefix.length) : '';
      }));
      return values.size === 1 ? Array.from(values)[0] : '';
    };
    state.color = pick('color');
    state.size = pick('size');
    return state;
  }
  function syncFmtState() {
    if (!fmtBar) return;
    const s = fmtState();
    fmtBar.querySelector('[data-fmt="bold"]').setAttribute('aria-pressed', String(s.bold));
    fmtBar.querySelector('[data-fmt="italic"]').setAttribute('aria-pressed', String(s.italic));
    const dot = fmtBar.querySelector('.tsn-fmt__dot');
    const hit = FMT_COLORS.find((c) => c[0] === s.color);
    dot.style.background = hit && hit[1] ? hit[1] : 'currentColor';
    fmtPanel.querySelectorAll('[data-color]').forEach((b) => b.setAttribute('aria-checked', String((b.dataset.color === 'default' ? !s.color : b.dataset.color === s.color))));
    fmtPanel.querySelectorAll('[data-size]').forEach((b) => b.setAttribute('aria-checked', String((b.dataset.size === 'normal' ? !s.size : b.dataset.size === s.size))));
  }

  function buildFmtBar() {
    if (fmtBar) return;
    const style = document.createElement('style');
    style.textContent = [
      '.tsn-fmt{position:fixed;z-index:2147483000;display:none;direction:rtl;font-family:inherit;user-select:none;-webkit-user-select:none;-webkit-touch-callout:none}',
      '.tsn-fmt[data-open="true"]{display:block}',
      '.tsn-fmt__bar{display:flex;align-items:center;gap:2px;padding:4px;background:#1f2937;border-radius:10px;box-shadow:0 6px 20px rgba(15,23,42,.28);opacity:0;transform:translateY(4px);transition:opacity .12s ease,transform .12s ease}',
      '.tsn-fmt[data-open="true"] .tsn-fmt__bar{opacity:1;transform:none}',
      '.tsn-fmt button{all:unset;box-sizing:border-box;min-width:34px;height:34px;display:inline-flex;align-items:center;justify-content:center;gap:4px;padding:0 8px;border-radius:7px;color:#f9fafb;font-size:14px;cursor:pointer;touch-action:manipulation}',
      '.tsn-fmt button:hover{background:rgba(255,255,255,.12)}',
      '.tsn-fmt button:focus-visible{outline:2px solid #93c5fd;outline-offset:1px}',
      '.tsn-fmt button[aria-pressed="true"]{background:#f9fafb;color:#111827}',
      '.tsn-fmt__sep{width:1px;height:20px;background:rgba(255,255,255,.2);margin:0 3px}',
      '.tsn-fmt__dot{width:14px;height:14px;border-radius:50%;border:2px solid #f9fafb}',
      '.tsn-fmt__panel{display:none;margin-top:6px;padding:6px;background:#fff;color:#111827;border:1px solid #e5e7eb;border-radius:10px;box-shadow:0 8px 24px rgba(15,23,42,.18)}',
      '.tsn-fmt__panel[data-kind]{display:flex;flex-wrap:wrap;gap:4px;max-width:236px}',
      '.tsn-fmt__panel button{color:#111827}',
      '.tsn-fmt__panel button:hover{background:#f3f4f6}',
      '.tsn-fmt__panel button[aria-checked="true"]{background:#e0e7ff;box-shadow:inset 0 0 0 1.5px #4f46e5}',
      '.tsn-fmt__swatch{width:30px;min-width:30px;height:30px;padding:0;border-radius:50%!important}',
      '.tsn-fmt__swatch span{width:18px;height:18px;border-radius:50%;border:1px solid rgba(0,0,0,.15)}',
      '.tsn-fmt__size{flex:1 1 100%;justify-content:space-between!important}',
      '@media (prefers-reduced-motion:reduce){.tsn-fmt__bar{transition:none}}'
    ].join('');
    document.head.appendChild(style);
    fmtBar = document.createElement('div');
    fmtBar.className = 'tsn-fmt';
    fmtBar.setAttribute('contenteditable', 'false');
    fmtBar.setAttribute('role', 'toolbar');
    fmtBar.setAttribute('aria-label', 'قالب‌بندی متن');
    fmtBar.innerHTML = '<div class="tsn-fmt__bar">' +
      '<button type="button" data-fmt="bold" aria-label="ضخیم" aria-pressed="false" title="ضخیم (Ctrl+B)"><b>B</b></button>' +
      '<button type="button" data-fmt="italic" aria-label="کج" aria-pressed="false" title="کج (Ctrl+I)"><i>I</i></button>' +
      '<span class="tsn-fmt__sep"></span>' +
      '<button type="button" data-fmt="color-menu" aria-label="رنگ متن" aria-haspopup="true" title="رنگ متن"><span class="tsn-fmt__dot"></span></button>' +
      '<button type="button" data-fmt="size-menu" aria-label="اندازه متن" aria-haspopup="true" title="اندازه متن">Aa</button>' +
      '</div><div class="tsn-fmt__panel"></div>';
    fmtPanel = fmtBar.querySelector('.tsn-fmt__panel');
    document.body.appendChild(fmtBar);
    // Keep the text selection alive: never let a toolbar press take focus.
    fmtBar.addEventListener('mousedown', (e) => e.preventDefault());
    fmtBar.addEventListener('pointerdown', (e) => { if (e.pointerType === 'mouse') e.preventDefault(); });
    fmtBar.addEventListener('click', (e) => {
      const btn = e.target.closest('button');
      if (!btn) return;
      e.preventDefault();
      if (btn.dataset.fmt === 'bold' || btn.dataset.fmt === 'italic') { applyFormat(btn.dataset.fmt); return; }
      if (btn.dataset.fmt === 'color-menu') { openFmtPanel('color'); return; }
      if (btn.dataset.fmt === 'size-menu') { openFmtPanel('size'); return; }
      if (btn.dataset.color) { applyFormat('color', btn.dataset.color); return; }
      if (btn.dataset.size) { applyFormat('size', btn.dataset.size); }
    });
  }
  function openFmtPanel(kind) {
    if (fmtPanel.dataset.kind === kind) { delete fmtPanel.dataset.kind; positionFmtBar(); return; }
    fmtPanel.dataset.kind = kind;
    fmtPanel.setAttribute('role', 'radiogroup');
    if (kind === 'color') {
      fmtPanel.innerHTML = FMT_COLORS.map(([key, hex, label]) =>
        '<button type="button" class="tsn-fmt__swatch" role="radio" aria-checked="false" data-color="' + key + '" aria-label="' + label + '" title="' + label + '"><span style="background:' + (hex || '#111827') + '"></span></button>').join('');
    } else {
      const scale = { sm: '12px', normal: '14px', lg: '17px', xl: '20px' };
      fmtPanel.innerHTML = FMT_SIZES.map(([key, label]) =>
        '<button type="button" class="tsn-fmt__size" role="radio" aria-checked="false" data-size="' + key + '"><span style="font-size:' + scale[key] + '">' + label + '</span></button>').join('');
    }
    syncFmtState();
    positionFmtBar();
  }
  function hideFmtBar() {
    if (!fmtBar || fmtBar.dataset.open !== 'true') return;
    fmtBar.dataset.open = 'false';
    delete fmtPanel.dataset.kind;
  }
  function fmtIsPhone() {
    const coarse = window.matchMedia('(pointer: coarse)').matches;
    const shortSide = Math.min(window.screen.width, window.screen.height);
    return window.innerWidth < 700 || (coarse && shortSide < 600);
  }
  function positionFmtBar() {
    const selection = window.getSelection();
    if (!fmtBar || fmtIsPhone() || fmtPointerDown || activeHighlightMode || isComposing || !selection || selection.rangeCount === 0 || selection.isCollapsed) { hideFmtBar(); return; }
    const range = selection.getRangeAt(0);
    if (!rangeIsInNote(range) || !fmtTextNodes(range).length) { hideFmtBar(); return; }
    const rects = range.getClientRects();
    const rect = rects.length ? range.getBoundingClientRect() : null;
    if (!rect || (!rect.width && !rect.height)) { hideFmtBar(); return; }
    fmtBar.dataset.open = 'true';
    const bw = fmtBar.offsetWidth;
    const bh = fmtBar.offsetHeight;
    const touch = window.matchMedia('(pointer: coarse)').matches;
    const margin = 8;
    // Touch devices show the OS selection menu above the text, so sit below.
    let top = touch ? rect.bottom + 14 : rect.top - bh - 10;
    if (top < margin) top = rect.bottom + 10;
    if (top + bh > window.innerHeight - margin) top = Math.max(margin, rect.top - bh - 10);
    let left = rect.left + rect.width / 2 - bw / 2;
    left = Math.min(Math.max(margin, left), Math.max(margin, window.innerWidth - bw - margin));
    fmtBar.style.top = Math.round(top) + 'px';
    fmtBar.style.left = Math.round(left) + 'px';
    syncFmtState();
  }
  function scheduleFmtBar() {
    if (fmtFrame || fmtIsPhone()) return;
    fmtFrame = window.requestAnimationFrame(() => { fmtFrame = 0; buildFmtBar(); positionFmtBar(); });
  }
  document.addEventListener('pointerdown', (e) => {
    if (fmtBar && fmtBar.contains(e.target)) return;
    fmtPointerDown = true;
    hideFmtBar();
  }, true);
  document.addEventListener('pointerup', () => { fmtPointerDown = false; window.setTimeout(scheduleFmtBar, 30); });
  document.addEventListener('pointercancel', () => { fmtPointerDown = false; });
  document.addEventListener('selectionchange', () => { if (!fmtPointerDown) scheduleFmtBar(); });
  document.addEventListener('keyup', (e) => { if (e.key === 'Escape') hideFmtBar(); });
  document.addEventListener('keydown', (e) => {
    if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return;
    const key = e.key.toLowerCase();
    if ((key === 'b' || key === 'i') && !fmtIsPhone() && rememberTextSelection()) {
      e.preventDefault();
      applyFormat(key === 'b' ? 'bold' : 'italic');
    }
  }, true);
  window.addEventListener('scroll', scheduleFmtBar, { passive: true, capture: true });
  window.addEventListener('resize', scheduleFmtBar);

  window.addEventListener('message', (event) => {
    if (event.source !== window.parent || event.data?.source !== SOURCE) return;
    if (event.data.action === 'add-block') insertBlock(event.data.blockType);
    if (event.data.action === 'add-image') insertImageBlock(event.data);
    if (event.data.action === 'open-formula') openFormula();
    if (event.data.action === 'insert-formula') insertFormula(event.data.formula, event.data.append);
    if (event.data.action === 'remember-selection') rememberTextSelection();
    if (event.data.action === 'set-highlight-mode') activeHighlightMode = event.data.mode === 'erase' ? 'erase' : event.data.color || null;
    if (event.data.action === 'clear-highlight-mode') activeHighlightMode = null;
    if (event.data.action === 'apply-highlight') applyHighlight(event.data.color);
    if (event.data.action === 'erase-highlight') eraseHighlight();
    if (event.data.action === 'remove-selected') removeSelectedBlock();
    if (event.data.action === 'set-review-markers') applyReviewMarkers(event.data.spots, event.data.tag, event.data.hint);
    if (event.data.action === 'clear-review-markers') clearReviewMarkers();
    if (event.data.action === 'scroll-to-review-spot') {
      document.querySelector('.tesanote-review-spot[data-spot-id="' + String(event.data.spotId).replace(/"/g, '') + '"]')
        ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
    if (event.data.action === 'undo') requestUndo();
    if (event.data.action === 'request-save' && event.data.autosave) {
      if (isComposing) {
        post('autosave-deferred');
        return;
      }
      const bodyHtml = serializeBody({ preserveLiveDom: true });
      post('save-payload', { bodyHtml, autosave: true });
      return;
    }
    if (event.data.action === 'request-save') {
      Promise.resolve(window.__tesanotePreparePagination?.({ force: true }))
        .finally(() => post('save-payload', { bodyHtml: serializeBody() }));
    }
  });

  function selectClickedBlock(event) {
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (target.closest('.tesanote-review-strip')) return;
    const resizeHandle = target.closest('.note-image-block__resize-handle');
    if (resizeHandle) {
      beginImageResize(event, resizeHandle.closest('.note-image-block'));
      return;
    }
    const cropHandle = target.closest('.note-image-block__crop-handle');
    if (cropHandle) {
      beginImageCrop(event, cropHandle.closest('.note-image-block'), cropHandle.dataset.cropSide);
      return;
    }
    const deleteButton = target.closest('.note-image-block__delete');
    if (deleteButton) {
      event.preventDefault();
      event.stopPropagation();
      deleteImageBlock(deleteButton.closest('.note-image-block'));
      return;
    }
    const block = target.closest('.page-content .note-block');
    if (block) selectBlock(block);
  }
  // Capture phase sees the interaction before contenteditable and nested
  // template elements can stop or reshape the bubbling click event.
  document.addEventListener('pointerdown', selectClickedBlock, true);
  document.addEventListener('mousedown', selectClickedBlock, true);
  document.addEventListener('click', (event) => {
    const link = event.target.closest?.('.table-of-contents__link');
    if (!link) return;
    event.preventDefault();
    const anchor = link.getAttribute('data-topic-anchor') || link.getAttribute('href')?.slice(1);
    const target = anchor && document.getElementById(anchor);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  document.addEventListener('pointermove', performImageResize);
  document.addEventListener('pointerup', endImageResize);
  document.addEventListener('pointermove', performImageCrop);
  document.addEventListener('pointerup', endImageCrop);
  // Keyboard or programmatic focus opens the source editor right away; a mouse
  // press waits for pointerup so a drag-selection (to highlight) is left alone.
  document.addEventListener('focusin', (event) => {
    const math = event.target.closest?.('.formula-box__math');
    if (!math || math.classList.contains('formula-box__math--editing')) return;
    if (fmtPointerDown || activeHighlightMode) return;
    enterFormulaEdit(math);
  });
  document.addEventListener('pointerup', (event) => {
    if (activeHighlightMode) return;
    const math = event.target.closest?.('.formula-box__math');
    if (!math || math.classList.contains('formula-box__math--editing')) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    enterFormulaEdit(math);
  });
  document.addEventListener('focusout', (event) => {
    const math = event.target.closest?.('.formula-box__math--editing');
    if (!math) return;
    const formulaBox = math.closest('.formula-box:not(.formula-box--query)');
    if (formulaBox) renderFormula(formulaBox, math.textContent);
  });
  document.addEventListener('input', (event) => {
    const formulaMath = event.target.closest?.('.formula-box__math--editing');
    if (formulaMath) {
      const formulaBox = formulaMath.closest('.formula-box:not(.formula-box--query)');
      if (formulaBox) formulaBox.dataset.formulaSource = formulaMath.textContent || '';
    }
    if (event.target.closest?.('.page-content')) {
      structuralEditIsLatest = false;
      reflowSoon();
      post('changed');
    }
  });
  document.addEventListener('selectionchange', rememberTextSelection);
  document.addEventListener('pointerup', () => scheduleActiveHighlight());
  document.addEventListener('keyup', (event) => {
    if (event.shiftKey || ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(event.key)) {
      scheduleActiveHighlight();
    }
  });
  document.addEventListener('paste', (event) => {
    if (!event.target.closest?.('[contenteditable="true"]')) return;
    event.preventDefault();
    const formulaBox = event.target.closest?.('.formula-box:not(.formula-box--query)');
    const pastedText = event.clipboardData?.getData('text/plain') || '';
    if (formulaBox) {
      renderFormula(formulaBox, pastedText);
      selectBlock(formulaBox.closest('.note-block'));
      reflowSoon();
      post('changed');
      return;
    }
    document.execCommand('insertText', false, pastedText);
  });
  // Native undo only knows about typed text, not inserted/removed blocks, so
  // fall back to the snapshot stack when native undo changes nothing.
  function requestUndo() {
    if (structuralEditIsLatest && undoStack.length) {
      undoStructural();
      return;
    }
    const wrapper = document.querySelector('.template-wrapper');
    const before = wrapper ? wrapper.innerHTML : '';
    let handledNatively = false;
    try { handledNatively = document.execCommand('undo'); } catch (error) { handledNatively = false; }
    if (handledNatively && wrapper && wrapper.innerHTML !== before) {
      reflowSoon();
      post('changed');
      return;
    }
    undoStructural();
  }

  document.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.shiftKey && event.key.toLowerCase() === 'z') {
      event.preventDefault();
      requestUndo();
      return;
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      post('save-requested');
    }
  });

  function addEditingHint() {
    // Outside .template-wrapper, so it is never saved into the note or printed in the PDF.
    if (document.getElementById('tesanote-editing-hint')) return;
    // Only phones are told to edit elsewhere; tablets and laptops already are.
    try {
      if (Math.min(window.screen.width, window.screen.height) >= 600) return;
    } catch (error) { return; }
    const band = document.createElement('div');
    band.id = 'tesanote-editing-hint';
    band.setAttribute('contenteditable', 'false');
    band.setAttribute('dir', 'rtl');
    band.style.cssText = 'background:#fff;padding:26px 16px 34px;margin:0;display:flex;justify-content:center;';
    const hint = document.createElement('div');
    hint.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;gap:10px;width:fit-content;max-width:100%;padding:10px 18px;white-space:nowrap;border-radius:999px;background:linear-gradient(180deg,#f5f9ff,#eaf2ff);border:1px solid #c7dcff;color:#1d4ed8;font:600 13.5px/1.8 Vazirmatn,Tahoma,sans-serif;text-align:center;box-shadow:0 1px 2px rgba(37,99,235,.10),0 6px 16px -8px rgba(37,99,235,.35);';
    const icon = document.createElement('span');
    icon.setAttribute('aria-hidden', 'true');
    icon.style.cssText = 'flex:none;display:inline-flex;width:22px;height:22px;border-radius:50%;background:#2563eb;color:#fff;align-items:center;justify-content:center;font:700 13px/1 Tahoma,sans-serif;';
    icon.textContent = 'i';
    const text = document.createElement('span');
    text.textContent = 'برای ویرایش جزوه از لپ‌تاپ استفاده کنید';
    hint.appendChild(icon);
    hint.appendChild(text);
    band.appendChild(hint);
    document.body.appendChild(band);
  }

  const start = () => {
    markEditable();
    addEditingHint();
    initializeImageCropFrames();
    const style = document.createElement('style');
    style.textContent = (
      '.tesanote-review-flag { position: relative; } .tesanote-review-flag::before { content: ""; ' +
      'position: absolute; inset-block: 4px; inset-inline-start: -14px; width: 2px; border-radius: 2px; ' +
      'background: linear-gradient(#f5b54a, rgba(245,181,74,0.15)); pointer-events: none; } ' +
      '.tesanote-review-strip { display: flex; justify-content: flex-end; align-items: center; gap: 6px; ' +
      'margin: 2px 0 6px; font-size: 11px; line-height: 1; direction: rtl; user-select: none; ' +
      'opacity: 0; transform: translateY(4px); transition: opacity .45s ease, transform .45s ease; } ' +
      '.tesanote-review-strip.is-in { opacity: 1; transform: none; } ' +
      '.tesanote-review-icon { display: inline-flex; align-items: flex-end; gap: 2px; height: 10px; } ' +
      '.tesanote-review-icon i { display: block; width: 2px; border-radius: 2px; background: #e0a43a; } ' +
      '.tesanote-review-icon i:nth-child(1) { height: 40%; } .tesanote-review-icon i:nth-child(2) { height: 100%; } ' +
      '.tesanote-review-icon i:nth-child(3) { height: 65%; } .tesanote-review-icon i:nth-child(4) { height: 30%; } ' +
      '.tesanote-review-spot { position: relative; border: 1px solid rgba(217,119,6,0.22); border-radius: 999px; ' +
      'background: rgba(254,243,199,0.7); color: #92400e; font: inherit; font-weight: 700; padding: 4px 9px; ' +
      'font-variant-numeric: tabular-nums; cursor: pointer; transition: background .18s ease, border-color .18s ease, ' +
      'box-shadow .18s ease; } .tesanote-review-spot:hover, .tesanote-review-spot:focus-visible { ' +
      'background: #fde68a; border-color: rgba(217,119,6,0.45); box-shadow: 0 4px 14px -6px rgba(217,119,6,0.55); outline: none; } ' +
      '.tesanote-review-spot[data-hint]:not([data-hint=""])::after { content: attr(data-hint); position: absolute; ' +
      'bottom: calc(100% + 8px); inset-inline-end: 0; white-space: nowrap; padding: 6px 10px; border-radius: 8px; ' +
      'background: rgba(17,24,39,0.92); color: #fff; font-size: 11px; font-weight: 500; letter-spacing: 0; ' +
      'box-shadow: 0 8px 24px -10px rgba(0,0,0,0.45); opacity: 0; transform: translateY(3px); pointer-events: none; ' +
      'transition: opacity .16s ease, transform .16s ease; z-index: 5; } ' +
      '.tesanote-review-spot:hover::after, .tesanote-review-spot:focus-visible::after { opacity: 1; transform: none; } ' +
      '@media (prefers-reduced-motion: reduce) { .tesanote-review-strip { transition: none; transform: none; } } ' +
      '.note-block { cursor: text; } .tesanote-editor-selected { outline: 2px solid ' +
      'color-mix(in srgb, var(--accent-color, #14b8a6) 70%, white); outline-offset: 5px; ' +
      'border-radius: 12px; } [contenteditable="true"]:focus { outline: 1px dashed color-mix(in ' +
      'srgb, var(--accent-color, #14b8a6) 60%, white); outline-offset: 3px; border-radius: 4px; ' +
      '} .note-image-block { cursor: default; } .note-image-block__controls { position: ' +
      'absolute; inset: 0; pointer-events: none; opacity: 0; transition: opacity 120ms ease; } ' +
      '.note-image-block:hover .note-image-block__controls, ' +
      '.note-image-block.tesanote-editor-selected .note-image-block__controls { opacity: 1; } ' +
      '.note-image-block__delete { position: absolute; top: 10px; left: 10px; width: 28px; ' +
      'height: 28px; display: flex; align-items: center; justify-content: center; border: none; ' +
      'border-radius: 999px; background: rgba(17, 17, 17, 0.72); color: #fff; font-size: 16px; ' +
      'line-height: 1; cursor: pointer; pointer-events: auto; box-shadow: 0 4px 10px rgba(0, 0, ' +
      '0, 0.25); transition: background 120ms ease, transform 120ms ease; } ' +
      '.note-image-block__delete:hover { background: rgba(220, 38, 38, 0.92); transform: ' +
      'scale(1.06); } .note-image-block__resize-handle { position: absolute; bottom: -7px; ' +
      'right: -7px; width: 16px; height: 16px; border-radius: 999px; background: #fff; border: ' +
      '2px solid color-mix(in srgb, var(--accent-color, #14b8a6) 80%, white); box-shadow: 0 2px ' +
      '6px rgba(0, 0, 0, 0.25); cursor: nwse-resize; pointer-events: auto; } ' +
      'body.tesanote-resizing-image, body.tesanote-resizing-image * { cursor: nwse-resize ' +
      '!important; user-select: none !important; } ' +
      '.note-image-block__crop-handle { position: absolute; background: #fff; border: 2px ' +
      'solid color-mix(in srgb, var(--accent-color, #14b8a6) 80%, white); box-shadow: 0 2px 6px ' +
      'rgba(0, 0, 0, 0.25); pointer-events: auto; border-radius: 4px; } ' +
      '.note-image-block__crop-handle--top, .note-image-block__crop-handle--bottom { left: 50%; ' +
      'width: 28px; height: 10px; margin-left: -14px; cursor: ns-resize; } ' +
      '.note-image-block__crop-handle--left, .note-image-block__crop-handle--right { top: 50%; ' +
      'width: 10px; height: 28px; margin-top: -14px; cursor: ew-resize; } ' +
      '.note-image-block__crop-handle--top { top: -5px; } ' +
      '.note-image-block__crop-handle--bottom { bottom: -5px; } ' +
      '.note-image-block__crop-handle--left { left: -5px; } ' +
      '.note-image-block__crop-handle--right { right: -5px; } ' +
      'body.tesanote-cropping-image, body.tesanote-cropping-image * { user-select: none ' +
      '!important; }'
    );
    document.head.appendChild(style);
    post('ready');
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start, { once: true });
  else start();
})();
</script>
""".replace("/*__TESANOTE_MATH_ENGINE__*/", math_engine_js())

    @staticmethod
    def save_user_edit(
        *, user, note_id, body_html: str, editor_revision: int, autosave: bool = False
    ) -> GeneratedNote:
        with transaction.atomic():
            note = GeneratedNote.objects.select_for_update().select_related("lecture").filter(id=note_id, user=user).first()
            if note is None:
                raise GeneratedNote.DoesNotExist
            if note.editor_revision != editor_revision:
                raise NoteEditConflictError("This note was updated in another session.")

            sanitized_body = GeneratedNoteService.sanitize_edited_body_html(body_html)
            note.body_html = sanitized_body
            note.semantic_payload = {}
            note.editor_revision += 1
            note.edited_at = timezone.now()
            note.generation_metadata = {
                **(note.generation_metadata or {}),
                "user_edited": True,
                "pdf_update_pending": True,
                "last_editor_save_at": note.edited_at.isoformat(),
            }
            note.save(
                update_fields=[
                    "body_html",
                    "semantic_payload",
                    "editor_revision",
                    "edited_at",
                    "generation_metadata",
                    "updated_at",
                ]
            )
            # Autosaves arrive every few seconds while typing; rendering a PDF
            # for each would flood the workers. The pending flag above makes the
            # PDF download/open path regenerate it lazily instead.
            if not autosave:
                transaction.on_commit(
                    lambda: GeneratedNoteService._queue_pdf_regeneration(note_id=str(note.id))
                )
            return note

    @staticmethod
    def sanitize_edited_body_html(body_html: str) -> str:
        sanitizer = _EditableNoteBodySanitizer()
        sanitizer.feed(str(body_html or ""))
        sanitizer.close()
        cleaned = sanitizer.rendered_html()
        if "template-wrapper" not in cleaned or "note-block" not in cleaned:
            raise NoteTemplateValidationError("Edited note must keep the document structure intact.")
        return validate_body_fragment(cleaned)

    MAX_IMAGE_UPLOAD_BYTES = 8 * 1024 * 1024
    MAX_IMAGE_DIMENSION = 2000

    @staticmethod
    def upload_image(*, user, note_id, file_obj) -> NoteImage:
        from PIL import Image, ImageOps, UnidentifiedImageError

        from apps.storage_lifecycle.quota import PersistentStorageQuotaService

        note = GeneratedNoteService.get_note(user=user, note_id=note_id)

        blocked, snapshot = PersistentStorageQuotaService.admission_is_blocked(user=user)
        if blocked:
            raise NoteImageQuotaExceededError(snapshot)

        raw_bytes = file_obj.read()
        if not raw_bytes:
            raise NoteImageValidationError("فایل تصویر خالی است.")
        if len(raw_bytes) > GeneratedNoteService.MAX_IMAGE_UPLOAD_BYTES:
            raise NoteImageValidationError("حجم تصویر نباید بیشتر از ۸ مگابایت باشد.")

        try:
            with Image.open(BytesIO(raw_bytes)) as opened:
                opened.verify()
            with Image.open(BytesIO(raw_bytes)) as image:
                image = ImageOps.exif_transpose(image)
                has_alpha = image.mode in {"RGBA", "LA"} or (
                    image.mode == "P" and "transparency" in image.info
                )
                image = image.convert("RGBA" if has_alpha else "RGB")
                image.thumbnail(
                    (
                        GeneratedNoteService.MAX_IMAGE_DIMENSION,
                        GeneratedNoteService.MAX_IMAGE_DIMENSION,
                    ),
                    Image.LANCZOS,
                )
                buffer = BytesIO()
                if has_alpha:
                    image.save(buffer, format="PNG", optimize=True)
                    content_type, extension = "image/png", "png"
                else:
                    image.save(buffer, format="JPEG", quality=85, optimize=True)
                    content_type, extension = "image/jpeg", "jpg"
                width, height = image.size
        except UnidentifiedImageError as exc:
            raise NoteImageValidationError("فایل انتخاب‌شده یک تصویر معتبر نیست.") from exc

        processed_bytes = buffer.getvalue()
        image_id = uuid4()
        storage_path = f"notes/{note.id}/images/{image_id}.{extension}"
        storage = get_storage_service()
        stored = storage.save_content(
            relative_path=storage_path, content=processed_bytes, content_type=content_type
        )
        StorageArtifactLifecycleService.register_persistent(
            user_id=str(user.id),
            lecture_id=str(note.lecture_id) if note.lecture_id else None,
            storage_path=stored.path,
            size_bytes=stored.size,
            artifact_type="note_image",
        )
        return NoteImage.objects.create(
            id=image_id,
            user=user,
            note=note,
            storage_path=stored.path,
            content_type=content_type,
            size_bytes=stored.size,
            width=width,
            height=height,
        )

    @staticmethod
    def open_image_stream(*, note_id, image_id):
        image = NoteImage.objects.filter(id=image_id, note_id=note_id).first()
        if image is None:
            return None, None
        storage = get_storage_service()
        try:
            stream = storage.open(image.storage_path, "rb")
        except (StorageNotFoundError, FileNotFoundError):
            return None, None
        return stream, image.content_type

    @staticmethod
    def delete_image(*, user, note_id, image_id) -> bool:
        image = NoteImage.objects.filter(id=image_id, note_id=note_id, user=user).first()
        if image is None:
            return False
        storage = get_storage_service()
        try:
            storage.delete(image.storage_path)
        except Exception:
            logger.exception(
                "note_image_delete_storage_failed",
                extra={"image_id": str(image.id), "storage_path": image.storage_path},
            )
        StorageArtifactLifecycleService.mark_deleted(storage_path=image.storage_path)
        image.delete()
        return True

    @staticmethod
    def _queue_pdf_regeneration(*, note_id: str) -> None:
        # Import locally to avoid the service/task import cycle during app startup.
        from apps.notes.tasks import generate_note_pdf_task

        generate_note_pdf_task.delay(note_id)

    @staticmethod
    def ensure_full_html_artifact(note: GeneratedNote, *, force: bool = False) -> str:
        storage = get_storage_service()
        body_changed = GeneratedNoteService._refresh_body_from_semantic_payload(note)
        rendering_fingerprint = GeneratedNoteService._rendering_fingerprint(note)
        metadata = note.generation_metadata or {}
        if (
            not force
            and not body_changed
            and note.html_path
            and storage.exists(note.html_path)
            and metadata.get("html_artifact_fingerprint") == rendering_fingerprint
        ):
            return note.html_path
        if not note.body_html.strip():
            return ""

        full_html = GeneratedNoteService.render_full_html_from_body(
            body_content=note.body_html,
            theme=normalize_note_theme(note.theme or note.lecture.processing_metadata.get("note_theme")),
            language_code=str((note.generation_metadata or {}).get("output_language") or note.lecture.output_language or "fa"),
            direction=str((note.generation_metadata or {}).get("output_direction") or "rtl"),
        )
        filename = f"generated-note-{note.variant}-v{GeneratedNoteService.PDF_ARTIFACT_VERSION}-{uuid4().hex}.html"
        relative = temporary_artifact_path(
            user_id=str(note.user_id),
            lecture_id=str(note.lecture_id),
            category="renders/html",
            filename=filename,
        )
        stored_file = storage.save_content(
            relative_path=relative,
            content=full_html.encode("utf-8"),
            content_type="text/html",
        )
        StorageArtifactLifecycleService.register_temporary(
            user_id=str(note.user_id),
            lecture_id=str(note.lecture_id),
            storage_path=stored_file.path,
            size_bytes=stored_file.size,
            artifact_type="rendered_note_html",
        )

        previous_html_path = note.html_path
        note.html_path = stored_file.path
        note.generation_metadata = {
            **metadata,
            "html_artifact_fingerprint": rendering_fingerprint,
        }
        note.save(update_fields=["html_path", "generation_metadata", "updated_at"])
        if previous_html_path and previous_html_path != stored_file.path:
            storage.delete(previous_html_path)
            StorageArtifactLifecycleService.mark_deleted(storage_path=previous_html_path)
        return stored_file.path

    @staticmethod
    def _render_pdf_with_browser(*, html_path: Path, pdf_path: Path) -> None:
        script_path = Path(settings.BASE_DIR) / "scripts" / "render_note_pdf.mjs"
        environment = os.environ.copy()
        environment["PLAYWRIGHT_MODULE_PATH"] = settings.NOTE_PDF_PLAYWRIGHT_MODULE
        if settings.NOTE_PDF_BROWSER_EXECUTABLE:
            environment["BROWSER_EXECUTABLE_PATH"] = settings.NOTE_PDF_BROWSER_EXECUTABLE

        result = subprocess.run(
            [
                settings.NOTE_PDF_NODE_EXECUTABLE,
                str(script_path),
                str(html_path),
                str(pdf_path),
            ],
            cwd=str(settings.BASE_DIR),
            env=environment,
            capture_output=True,
            text=True,
            timeout=settings.NOTE_PDF_RENDER_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout or "Unknown browser PDF render error."
            raise RuntimeError(detail)

    @staticmethod
    def ensure_pdf_artifact(note: GeneratedNote) -> str:
        if note.lecture.status == "PROCESSING":
            LectureProcessingStateService.mark_generating_pdf(
                lecture=note.lecture,
                progress=max(note.lecture.processing_progress, 90),
            )
        storage = get_storage_service()
        body_changed = GeneratedNoteService._refresh_body_from_semantic_payload(note)
        rendering_fingerprint = GeneratedNoteService._rendering_fingerprint(note)
        existing_pdf_is_current = not body_changed and GeneratedNoteService.pdf_artifact_is_current(
            note,
            rendering_fingerprint=rendering_fingerprint,
        )
        if existing_pdf_is_current:
            GeneratedNoteService._sync_pdf_record(note=note, storage_path=note.pdf_path)
            return note.pdf_path

        html_relative = GeneratedNoteService.ensure_full_html_artifact(
            note,
            force=not existing_pdf_is_current,
        )
        if not html_relative:
            return ""

        filename = f"generated-note-{note.variant}-v{GeneratedNoteService.PDF_ARTIFACT_VERSION}-{uuid4().hex}.pdf"
        relative = f"{settings.LECTURE_UPLOAD_ROOT}/{note.lecture_id}/notes/{filename}"
        with NamedTemporaryFile(suffix=".html", delete=False) as html_temp:
            html_temp_path = Path(html_temp.name)
        with NamedTemporaryFile(suffix=".pdf", delete=False) as pdf_temp:
            pdf_temp_path = Path(pdf_temp.name)
        try:
            with storage.materialize_to_temp(html_relative) as html_path:
                with provider_concurrency_slot(
                    provider_key="pdf-render",
                    label="PDF rendering",
                    max_concurrency=settings.PDF_RENDER_MAX_CONCURRENCY,
                    wait_timeout_seconds=settings.PDF_RENDER_SLOT_WAIT_SECONDS,
                    poll_interval_seconds=settings.PDF_RENDER_SLOT_POLL_SECONDS,
                    lease_seconds=max(30, settings.NOTE_PDF_RENDER_TIMEOUT_SECONDS + 30),
                    stage="GENERATING_PDF",
                ):
                    GeneratedNoteService._render_pdf_with_browser(
                        html_path=Path(html_path), pdf_path=pdf_temp_path
                    )
            stored_file = storage.save_file(
                relative_path=relative,
                local_path=str(pdf_temp_path),
                content_type="application/pdf",
            )
        except Exception as exc:
            ErrorRecordingService.record_processing_error(
                user=note.user,
                lecture=note.lecture,
                stage="GENERATING_PDF",
                error_type=exc.__class__.__name__,
                error_message=str(exc),
                exception=exc,
                metadata={"note_id": str(note.id), "variant": note.variant},
            )
            logger.exception("note_pdf_render_failed", extra={"note_id": str(note.id), "lecture_id": str(note.lecture_id)})
            return ""
        finally:
            if html_temp_path.exists():
                html_temp_path.unlink(missing_ok=True)
            if pdf_temp_path.exists():
                pdf_temp_path.unlink(missing_ok=True)

        previous_pdf_path = note.pdf_path
        StorageArtifactLifecycleService.register_persistent(
            user_id=str(note.user_id),
            lecture_id=str(note.lecture_id),
            storage_path=stored_file.path,
            size_bytes=stored_file.size,
            artifact_type="final_note_pdf",
        )
        note.pdf_path = stored_file.path
        note.generation_metadata = {
            **(note.generation_metadata or {}),
            "pdf_artifact_version": GeneratedNoteService.PDF_ARTIFACT_VERSION,
            "pdf_artifact_fingerprint": rendering_fingerprint,
            "pdf_update_pending": False,
        }
        note.save(update_fields=["pdf_path", "generation_metadata", "updated_at"])
        GeneratedNoteService._sync_pdf_record(note=note, storage_path=stored_file.path)
        if previous_pdf_path and previous_pdf_path != stored_file.path:
            storage.delete(previous_pdf_path)
            StorageArtifactLifecycleService.mark_deleted(storage_path=previous_pdf_path)
        return stored_file.path

    @staticmethod
    def pdf_artifact_exists(note: GeneratedNote) -> bool:
        storage = get_storage_service()
        if note.pdf_path and GeneratedNoteService._stored_pdf_exists(storage, note.pdf_path):
            return True

        # Older failed rebuilds cleared GeneratedNote.pdf_path before rendering.
        # NotePdf still retains the last successful artifact, so recover it.
        try:
            recovered_path = str(note.pdf_record.storage_path or "")
        except NotePdf.DoesNotExist:
            recovered_path = ""
        if not recovered_path or not storage.exists(recovered_path):
            return False
        note.pdf_path = recovered_path
        note.save(update_fields=["pdf_path", "updated_at"])
        return True

    PDF_EXISTS_CACHE_SECONDS = 300

    @staticmethod
    def _stored_pdf_exists(storage, path: str) -> bool:
        """`storage.exists` is an object-store round trip (~100-800 ms) on every
        note open. Only a positive answer is cached, briefly: a rebuild deletes
        the previous PDF only after `note.pdf_path` already points at the new
        one, so a cached "exists" never describes the path being served."""
        cache_key = f"note-pdf-exists:{path}"
        if cache.get(cache_key):
            return True
        found = storage.exists(path)
        if found:
            cache.set(cache_key, True, GeneratedNoteService.PDF_EXISTS_CACHE_SECONDS)
        return found

    @staticmethod
    def pdf_artifact_is_current(note: GeneratedNote, *, rendering_fingerprint: str | None = None) -> bool:
        if not GeneratedNoteService.pdf_artifact_exists(note):
            return False
        try:
            artifact_version = int((note.generation_metadata or {}).get("pdf_artifact_version", 0) or 0)
        except (TypeError, ValueError):
            return False
        if artifact_version != GeneratedNoteService.PDF_ARTIFACT_VERSION:
            return False
        try:
            current_fingerprint = rendering_fingerprint or GeneratedNoteService._rendering_fingerprint(note)
        except OSError:
            logger.exception("note_pdf_artifact_fingerprint_failed", extra={"note_id": str(note.id)})
            return False
        return (note.generation_metadata or {}).get("pdf_artifact_fingerprint") == current_fingerprint

    @staticmethod
    @transaction.atomic
    def _sync_pdf_record(*, note: GeneratedNote, storage_path: str) -> NotePdf:
        pdf_record, created = NotePdf.objects.get_or_create(
            note=note,
            defaults={"user": note.user, "storage_path": storage_path},
        )
        if not created and pdf_record.storage_path != storage_path:
            pdf_record.storage_path = storage_path
            pdf_record.save(update_fields=["storage_path", "updated_at"])
        return pdf_record

    @staticmethod
    def open_pdf_stream(*, note: GeneratedNote):
        if not note.pdf_path:
            return None
        storage = get_storage_service()
        if not storage.exists(note.pdf_path):
            return None
        return storage.open(note.pdf_path, "rb")

    @staticmethod
    def increment_download_count(*, note: GeneratedNote) -> NotePdf:
        pdf_record = GeneratedNoteService._sync_pdf_record(note=note, storage_path=note.pdf_path)
        pdf_record.download_count += 1
        pdf_record.save(update_fields=["download_count", "updated_at"])
        return pdf_record
