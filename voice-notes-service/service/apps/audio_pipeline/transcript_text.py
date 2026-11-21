"""Plain transcript text from whatever shape a speech provider answered with."""

from __future__ import annotations

import json
import re

# Some gateways answer "text" requests with a JSON document ({"text": "...", "usage": ...}); only the
# spoken words belong in a transcript.
_LOOSE_TEXT_FIELD = re.compile(r'^\s*\{\s*"text"\s*:\s*"(.*?)"\s*(?:,\s*"[A-Za-z_]+"\s*:.*)?\}\s*$', re.DOTALL)


def unwrap_transcript_text(value: str) -> str:
    text = str(value or "")
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}") and '"text"' in stripped[:20]):
        return text
    try:
        payload = json.loads(stripped)
    except ValueError:
        match = _LOOSE_TEXT_FIELD.match(stripped)
        return match.group(1).replace('\\"', '"').replace("\\n", "\n").strip() if match else text
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return payload["text"].strip()
    return text


_EMBEDDED_JSON_TEXT = re.compile(r'\{\s*"text"\s*:\s*"(.*?)"\s*,\s*"usage"\s*:.*?\}', re.DOTALL)


def unwrap_embedded_transcript_json(value: str) -> str:
    """Older lectures stored the provider's JSON wrapper inside the transcript; take the words out."""
    text = str(value or "")
    if '"usage"' not in text:
        return text
    return _EMBEDDED_JSON_TEXT.sub(lambda match: match.group(1).strip(), text)
