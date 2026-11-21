from __future__ import annotations

import base64
from functools import lru_cache
import io
from pathlib import Path
import re

from django.conf import settings

from apps.notes.themes import normalize_note_theme
from apps.notes.validators import NoteTemplateValidationError, validate_body_fragment


class NoteTemplateEngine:
    placeholder = "{{CONTENT}}"
    theme_logo_placeholder = "{{THEME_LOGO_SRC}}"
    tesanote_letter_placeholder = "{{TESANOTE_LETTER_SRC}}"
    caveat_font_placeholder = "{{CAVEAT_FONT_SRC}}"
    vazirmatn_font_placeholder = "{{VAZIRMATN_FONT_SRC}}"
    template_name = "base-note-template.html"
    international_template_name = "base-note-template-international.html"

    def __init__(self, *, template_path: Path | None = None) -> None:
        self.base_dir = Path(settings.BASE_DIR)
        self.project_root = self._resolve_project_root()
        self.custom_template_path = template_path
        self.template_path = template_path or (self.project_root / "templates" / self.template_name)

    def _resolve_project_root(self) -> Path:
        candidates = [
            self.base_dir,
            self.base_dir.parent,
        ]
        for candidate in candidates:
            if (candidate / "templates" / self.template_name).exists() and (candidate / "public").exists():
                return candidate
        return self.base_dir

    def load_template(self, *, language_code: str = "fa") -> str:
        template_path = self._template_path_for_language(language_code=language_code)
        try:
            template = template_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:  # pragma: no cover - configuration issue
            raise NoteTemplateValidationError(f"Base note template was not found at {template_path}.") from exc

        placeholder_count = template.count(self.placeholder)
        if placeholder_count != 1:
            raise NoteTemplateValidationError(
                f"Base note template must contain exactly one {self.placeholder} placeholder."
            )
        return template

    def _template_path_for_language(self, *, language_code: str) -> Path:
        if self.custom_template_path is not None:
            return self.custom_template_path
        if str(language_code or "fa").lower() == "fa":
            return self.project_root / "templates" / self.template_name
        return self.project_root / "templates" / self.international_template_name

    def render(self, *, body_content: str, theme: str = "green") -> str:
        return self.render_with_options(body_content=body_content, minify=False, theme=theme)

    def render_with_options(
        self,
        *,
        body_content: str,
        minify: bool = False,
        theme: str = "green",
        language_code: str = "fa",
        direction: str = "rtl",
        for_editor: bool = False,
    ) -> str:
        """Render a full note page.

        ``for_editor`` builds the lighter document the in-app editor loads: fonts as WOFF2 instead
        of TTF, and the page-header logo shrunk and inlined once (a script fills every page), not
        once per page. The PDF and the saved document never use it.
        """
        template = self.load_template(language_code=language_code)
        validated_content = validate_body_fragment(body_content)
        normalized_theme = normalize_note_theme(theme)
        theme_logo_src = self._theme_logo_uri(normalized_theme)
        tesanote_letter_src = self._tesanote_letter_uri()
        caveat_font_src = self._caveat_font_uri()
        vazirmatn_font_src = self._vazirmatn_font_uri()
        themed_template = re.sub(
            r'(<html\b[^>]*\bdata-theme=")[^"]+(")',
            rf"\1{normalized_theme}\2",
            template,
            count=1,
        )
        themed_template = re.sub(
            r'(<html\b[^>]*\blang=")[^"]+(")',
            rf"\1{language_code}\2",
            themed_template,
            count=1,
        )
        themed_template = re.sub(
            r'(<html\b[^>]*\bdir=")[^"]+(")',
            rf"\1{direction}\2",
            themed_template,
            count=1,
        )
        themed_template = re.sub(
            r"(body\s*\{[\s\S]*?direction:\s*)(rtl|ltr)(;)",
            rf"\1{direction}\3",
            themed_template,
            count=1,
        )
        rendered = themed_template.replace(self.placeholder, validated_content, 1)
        if for_editor:
            return self._editor_assets(rendered, theme=normalized_theme, letter_src=tesanote_letter_src)
        rendered = rendered.replace(self.theme_logo_placeholder, theme_logo_src)
        rendered = rendered.replace(self.tesanote_letter_placeholder, tesanote_letter_src)
        rendered = rendered.replace(self.caveat_font_placeholder, caveat_font_src)
        rendered = rendered.replace(self.vazirmatn_font_placeholder, vazirmatn_font_src)
        return self.minify(rendered) if minify else rendered

    # A 1x1 transparent GIF: what each header logo holds until the editor script sets the real one.
    _BLANK_IMAGE = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    _EDITOR_LOGO_HEIGHT = 120  # twice the largest header logo height (60px), sharp on retina

    def _editor_assets(self, rendered: str, *, theme: str, letter_src: str) -> str:
        fonts = (
            (self.vazirmatn_font_placeholder, self._editor_font_uri("Vazirmatn-VariableFont_wght.woff2"), self._vazirmatn_font_uri),
            (self.caveat_font_placeholder, self._editor_font_uri("Caveat-VariableFont_wght.woff2"), self._caveat_font_uri),
        )
        for placeholder, woff2_uri, ttf_uri in fonts:
            if woff2_uri:
                rendered = re.sub(
                    re.escape(f"url('{placeholder}')") + r"\s*format\('truetype'\)",
                    f"url('{woff2_uri}') format('woff2')",
                    rendered,
                )
            rendered = rendered.replace(placeholder, ttf_uri())
        rendered = rendered.replace(self.tesanote_letter_placeholder, letter_src)
        logo_uri = self._editor_logo_uri(theme)
        rendered = rendered.replace(self.theme_logo_placeholder, self._BLANK_IMAGE)
        logo_script = (
            "<script id=\"tesanote-editor-logo\">(function(){var src='" + logo_uri + "';"
            "function fill(){document.querySelectorAll('.page-header__brand-logo').forEach(function(img){"
            "if(img.getAttribute('src')!==src)img.setAttribute('src',src);});}"
            "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',fill);else fill();"
            "window.__tesanoteFillLogo=fill;})();</script>"
        )
        return rendered.replace("</body>", f"{logo_script}</body>", 1)

    def _editor_font_uri(self, file_name: str) -> str:
        path = self.project_root / "public" / "fonts" / "editor" / file_name
        try:
            return _cached_data_uri(str(path), "font/woff2")
        except OSError:
            return ""

    def _editor_logo_uri(self, theme: str) -> str:
        path = self.project_root / "public" / "logocolors" / f"{theme}logo.png"
        try:
            return _cached_small_png_uri(str(path), self._EDITOR_LOGO_HEIGHT)
        except Exception:  # Pillow missing or an unreadable file: the full logo still works
            return self._image_data_uri(path)

    def _theme_logo_uri(self, theme: str) -> str:
        logo_path = self.project_root / "public" / "logocolors" / f"{theme}logo.png"
        return self._image_data_uri(logo_path)

    def _tesanote_letter_uri(self) -> str:
        letter_path = self.project_root / "public" / "tesanote-letter.png"
        return self._image_data_uri(letter_path)

    def _caveat_font_uri(self) -> str:
        font_path = self.project_root / "public" / "fonts" / "Caveat" / "Caveat-VariableFont_wght.ttf"
        encoded = base64.b64encode(font_path.read_bytes()).decode("ascii")
        return f"data:font/ttf;base64,{encoded}"

    def _vazirmatn_font_uri(self) -> str:
        font_path = (
            self.project_root
            / "public"
            / "fonts"
            / "Caveat,Vazirmatn"
            / "Vazirmatn"
            / "Vazirmatn-VariableFont_wght.ttf"
        )
        encoded = base64.b64encode(font_path.read_bytes()).decode("ascii")
        return f"data:font/ttf;base64,{encoded}"

    @staticmethod
    def _image_data_uri(path: Path) -> str:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"

    @staticmethod
    def minify(html: str) -> str:
        return re.sub(r">\s+<", "><", html).strip()


@lru_cache(maxsize=16)
def _cached_data_uri(path: str, content_type: str) -> str:
    return f"data:{content_type};base64,{base64.b64encode(Path(path).read_bytes()).decode('ascii')}"


@lru_cache(maxsize=16)
def _cached_small_png_uri(path: str, height: int) -> str:
    from PIL import Image

    with Image.open(path) as image:
        image = image.convert("RGBA")
        if image.height > height:
            width = max(1, round(image.width * height / image.height))
            image = image.resize((width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
    return f"data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode('ascii')}"
