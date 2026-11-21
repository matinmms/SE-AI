"""Text from the pictures on slide pages.

Slides are often exported as one picture per page (or carry screenshots and infographics), so the
names, labels and numbers on them are not in the file's text layer. This reads them with RapidOCR, a
free CPU library, in a short-lived child process (see ocr_worker.py). Any failure returns no text, so
slides keep working exactly as before.

Limits: the bundled models read English/Latin text, digits and symbols; Persian script inside a
picture is not recognised.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from io import BytesIO
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("tesanote")

WORKER_SCRIPT = Path(__file__).with_name("ocr_worker.py")
MAX_IMAGE_SIDE = 800


def _lower_priority() -> None:
    try:
        os.nice(10)
    except OSError:
        pass


def parse_tesseract_tsv(tsv: str, *, minimum_confidence: float) -> list[dict[str, object]]:
    """Group Tesseract's word rows into text lines, dropping low-confidence words."""
    lines: dict[tuple[str, str, str], list[tuple[str, float]]] = {}
    for row in tsv.splitlines()[1:]:
        fields = row.split("\t")
        if len(fields) < 12 or fields[0] != "5":
            continue
        try:
            confidence = float(fields[10])
        except ValueError:
            continue
        word = fields[11].strip()
        if word and confidence >= minimum_confidence * 100:
            lines.setdefault((fields[2], fields[3], fields[4]), []).append((word, confidence))
    return [
        {"text": " ".join(word for word, _ in words), "confidence": sum(conf for _, conf in words) / len(words) / 100}
        for words in lines.values()
    ]


class OCRExtractor:
    def __init__(self, *, run_worker=None, run_fallback=None) -> None:
        # ``run_worker(manifest_path, result_path) -> bool`` is replaceable so tests need no OCR engine.
        self._run_worker = run_worker or self._run_child_process
        self._run_fallback = run_fallback or self._run_tesseract

    @staticmethod
    def _run_tesseract(manifest_path: Path, result_path: Path) -> bool:
        """Slower fallback when RapidOCR cannot run on this machine; reads English and, when installed, Persian."""
        binary = shutil.which("tesseract")
        if binary is None:
            return False
        try:
            languages = subprocess.run(  # noqa: S603
                [binary, "--list-langs"], capture_output=True, text=True, timeout=15, check=False
            ).stdout.split()
        except (subprocess.TimeoutExpired, OSError):
            return False
        language = "+".join(code for code in ("eng", "fas") if code in languages)
        if not language:
            return False
        minimum = float(settings.SLIDE_OCR_MIN_CONFIDENCE)
        results: dict[str, list[dict[str, object]]] = {}
        deadline = time.monotonic() + int(settings.SLIDE_OCR_TIMEOUT_SECONDS)
        for item in json.loads(manifest_path.read_text(encoding="utf-8")):
            if time.monotonic() > deadline:
                break
            try:
                completed = subprocess.run(  # noqa: S603
                    [binary, item["path"], "stdout", "-l", language, "--psm", "11", "tsv"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    preexec_fn=_lower_priority,
                )
            except (subprocess.TimeoutExpired, OSError):
                continue
            if completed.returncode == 0:
                results[str(item["id"])] = parse_tesseract_tsv(completed.stdout, minimum_confidence=minimum)
        result_path.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
        return True

    @staticmethod
    def enabled() -> bool:
        return bool(getattr(settings, "SLIDE_OCR_ENABLED", True))

    @staticmethod
    def _run_child_process(manifest_path: Path, result_path: Path) -> bool:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed interpreter and script, no user input in the command
                [
                    sys.executable,
                    str(WORKER_SCRIPT),
                    str(manifest_path),
                    str(result_path),
                    str(max(1, int(settings.SLIDE_OCR_THREADS))),
                ],
                capture_output=True,
                timeout=int(settings.SLIDE_OCR_TIMEOUT_SECONDS),
                check=False,
                # Lower priority, so transcription and web requests keep the CPU.
                preexec_fn=_lower_priority,
            )
        except (subprocess.TimeoutExpired, OSError):
            logger.exception("slide_ocr_worker_failed")
            return False
        if completed.returncode == 3:
            logger.warning("slide_ocr_unavailable", extra={"reason": "rapidocr is not installed"})
        return completed.returncode == 0

    @staticmethod
    def _prepare(image_bytes: bytes) -> bytes | None:
        """A bounded JPEG of the picture, or None when it is unreadable or too small to hold text."""
        try:
            from PIL import Image

            with Image.open(BytesIO(image_bytes)) as image:
                if min(image.size) < int(settings.SLIDE_OCR_MIN_IMAGE_PX):
                    return None
                prepared = image.convert("RGB")
                prepared.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
                buffer = BytesIO()
                prepared.save(buffer, format="JPEG", quality=88)
                return buffer.getvalue()
        except Exception:  # noqa: BLE001 - formats Pillow cannot open (for example EMF) are skipped
            return None

    def extract_pages(self, *, pages: list[tuple[int, list[dict[str, object]]]]) -> dict[int, list[dict[str, object]]]:
        """Page number -> OCR blocks ({"page", "image_index", "lines": [{"text", "confidence"}]}).

        A picture that repeats on several pages (a logo, a footer) is read once; later copies add nothing.
        """
        if not self.enabled():
            return {}
        seen: set[str] = set()
        queue: list[tuple[str, int, object, bytes]] = []
        limit = max(0, int(settings.SLIDE_OCR_MAX_IMAGES))
        for page_number, images in pages:
            for image in images:
                raw = image.get("bytes")
                if not raw or len(queue) >= limit:
                    continue
                digest = hashlib.sha1(raw).hexdigest()  # noqa: S324 - a cache key, not security
                if digest in seen:
                    continue
                seen.add(digest)
                prepared = self._prepare(raw)
                if prepared is not None:
                    queue.append((f"{page_number}:{len(queue)}", page_number, image.get("index"), prepared))
        if not queue:
            return {}
        minimum = float(settings.SLIDE_OCR_MIN_CONFIDENCE)
        try:
            with tempfile.TemporaryDirectory(prefix="tesanote-ocr-") as workspace:
                root = Path(workspace)
                manifest = []
                for key, _page, _index, prepared in queue:
                    path = root / f"{key.replace(':', '-')}.jpg"
                    path.write_bytes(prepared)
                    manifest.append({"id": key, "path": str(path)})
                manifest_path, result_path = root / "manifest.json", root / "result.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                if not self._run_worker(manifest_path, result_path) or not result_path.exists():
                    result_path.unlink(missing_ok=True)
                    if not self._run_fallback(manifest_path, result_path) or not result_path.exists():
                        return {}
                results = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - OCR is an enrichment; slides continue without it
            logger.exception("slide_ocr_failed")
            return {}
        output: dict[int, list[dict[str, object]]] = {}
        for key, page_number, image_index, _prepared in queue:
            lines = [line for line in results.get(key, []) if float(line.get("confidence", 0)) >= minimum]
            if lines:
                output.setdefault(page_number, []).append(
                    {"page": page_number, "image_index": image_index, "lines": lines}
                )
        return output
