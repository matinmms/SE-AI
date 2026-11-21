from __future__ import annotations

SUPPORTED_LANGUAGES = [
    {"code": "fa", "label_fa": "فارسی", "label_en": "Persian", "stt_code": "fa", "rtl": True},
    {"code": "en", "label_fa": "انگلیسی", "label_en": "English", "stt_code": "en", "rtl": False},
    {"code": "ar", "label_fa": "عربی", "label_en": "Arabic", "stt_code": "ar", "rtl": True},
    {"code": "tr", "label_fa": "ترکی", "label_en": "Turkish", "stt_code": "tr", "rtl": False},
    {"code": "fr", "label_fa": "فرانسوی", "label_en": "French", "stt_code": "fr", "rtl": False},
    {"code": "de", "label_fa": "آلمانی", "label_en": "German", "stt_code": "de", "rtl": False},
]

DEFAULT_INPUT_LANGUAGE = "fa"
DEFAULT_OUTPUT_LANGUAGE = "fa"
DEFAULT_NOTE_DETAIL_MODE = "detailed"
SUPPORTED_LANGUAGE_CODES = tuple(language["code"] for language in SUPPORTED_LANGUAGES)
LANGUAGE_CHOICES = tuple((code, code) for code in SUPPORTED_LANGUAGE_CODES)
NOTE_DETAIL_MODES = (
    ("detailed", "Detailed"),
    ("summarized", "Summarized"),
)


def language_config(code: str | None) -> dict[str, object]:
    normalized = str(code or DEFAULT_INPUT_LANGUAGE).strip().lower()
    for language in SUPPORTED_LANGUAGES:
        if language["code"] == normalized:
            return language
    return next(language for language in SUPPORTED_LANGUAGES if language["code"] == DEFAULT_INPUT_LANGUAGE)


def stt_language_code(code: str | None) -> str:
    return str(language_config(code).get("stt_code") or DEFAULT_INPUT_LANGUAGE)


def is_rtl_language(code: str | None) -> bool:
    return bool(language_config(code).get("rtl", False))


def language_label(code: str | None, *, locale: str = "fa") -> str:
    config = language_config(code)
    key = "label_fa" if locale == "fa" else "label_en"
    return str(config.get(key) or config.get("code") or "")
