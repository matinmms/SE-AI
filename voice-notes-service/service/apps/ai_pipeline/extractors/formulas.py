from __future__ import annotations

from io import BytesIO

try:
    from pix2tex.cli import LatexOCR  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    LatexOCR = None

try:
    from PIL import Image  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    Image = None


class FormulaExtractor:
    def __init__(self) -> None:
        self._model = None

    def _get_model(self):
        if LatexOCR is None:
            return None
        if self._model is None:
            self._model = LatexOCR()
        return self._model

    def extract(self, *, page_number: int, images: list[dict[str, object]]) -> list[dict[str, object]]:
        model = self._get_model()
        if model is None or Image is None:
            return []

        formulas: list[dict[str, object]] = []
        for image in images:
            image_bytes = image.get("bytes")
            if not image_bytes:
                continue
            try:
                latex = model(Image.open(BytesIO(image_bytes)))
            except Exception:
                continue
            if latex:
                formulas.append(
                    {
                        "page": page_number,
                        "latex": latex,
                        "confidence": 0.0,
                    }
                )
        return formulas
