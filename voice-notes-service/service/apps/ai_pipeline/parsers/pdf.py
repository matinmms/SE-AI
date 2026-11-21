from __future__ import annotations

from io import BytesIO

from apps.ai_pipeline.exceptions import PipelineError
from apps.ai_pipeline.types import ParsedSlidePage, ParsedSlidesDocument

try:
    import fitz  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    fitz = None


class PDFSlideParser:
    def parse(self, *, file_bytes: bytes, include_images: bool = True) -> ParsedSlidesDocument:
        if fitz is None:
            raise PipelineError(stage="PDF_PARSE", message="PyMuPDF is not installed.")

        try:
            document = fitz.open(stream=file_bytes, filetype="pdf")
        except Exception as exc:  # pragma: no cover - parser failure depends on file content
            raise PipelineError(stage="PDF_PARSE", message=f"Unable to open PDF: {exc}") from exc

        pages: list[ParsedSlidePage] = []
        raw_pdf_text: list[dict[str, object]] = []
        slide_pages_raw: list[dict[str, object]] = []

        for index, page in enumerate(document, start=1):
            raw_text = page.get_text("text")
            images: list[dict[str, object]] = []
            if include_images:
                for image_index, image in enumerate(page.get_images(full=True), start=1):
                    xref = image[0]
                    image_info = document.extract_image(xref)
                    images.append(
                        {
                            "page": index,
                            "index": image_index,
                            "ext": image_info.get("ext", ""),
                            "width": image_info.get("width"),
                            "height": image_info.get("height"),
                            "bytes": image_info.get("image"),
                        }
                    )

            structured_text = "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())
            pages.append(
                ParsedSlidePage(
                    page_number=index,
                    text=structured_text,
                    raw_extracted_text=raw_text,
                    structured_text=structured_text,
                    images=images,
                )
            )
            raw_pdf_text.append({"page": index, "text": raw_text})
            slide_pages_raw.append({"page_number": index, "raw_extracted_text": raw_text, "images": len(images)})

        document.close()
        return ParsedSlidesDocument(pages=pages, raw_pdf_text=raw_pdf_text, slide_pages_raw=slide_pages_raw)

    def render_thumbnails(self, *, file_bytes: bytes) -> list[tuple[int, bytes]]:
        if fitz is None:
            raise PipelineError(stage="THUMBNAILS", message="PyMuPDF is not installed.")

        document = fitz.open(stream=file_bytes, filetype="pdf")
        thumbnails: list[tuple[int, bytes]] = []
        for index, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
            thumbnails.append((index, pixmap.tobytes("png")))
        document.close()
        return thumbnails
