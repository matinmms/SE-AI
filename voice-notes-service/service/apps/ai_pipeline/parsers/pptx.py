from __future__ import annotations

from io import BytesIO

from apps.ai_pipeline.exceptions import PipelineError
from apps.ai_pipeline.types import ParsedSlidePage, ParsedSlidesDocument

try:
    from pptx import Presentation  # type: ignore[import-not-found]
    from pptx.enum.shapes import MSO_SHAPE_TYPE  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    Presentation = None
    MSO_SHAPE_TYPE = None


class PPTXSlideParser:
    def parse(self, *, file_bytes: bytes, include_images: bool = True) -> ParsedSlidesDocument:
        if Presentation is None:
            raise PipelineError(stage="PPTX_PARSE", message="python-pptx is not installed.")

        try:
            presentation = Presentation(BytesIO(file_bytes))
        except Exception as exc:  # pragma: no cover
            raise PipelineError(stage="PPTX_PARSE", message=f"Unable to open PPTX: {exc}") from exc

        pages: list[ParsedSlidePage] = []
        slide_pages_raw: list[dict[str, object]] = []

        for index, slide in enumerate(presentation.slides, start=1):
            texts: list[str] = []
            images: list[dict[str, object]] = []
            tables: list[dict[str, object]] = []

            for shape_index, shape in enumerate(slide.shapes, start=1):
                if getattr(shape, "has_text_frame", False):
                    text = shape.text.strip()
                    if text:
                        texts.append(text)

                if getattr(shape, "has_table", False):
                    rows = []
                    for row in shape.table.rows:
                        rows.append([cell.text.strip() for cell in row.cells])
                    tables.append({"page": index, "shape_index": shape_index, "data": rows})

                if include_images and MSO_SHAPE_TYPE is not None and shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    images.append(
                        {
                            "page": index,
                            "index": shape_index,
                            "width": int(shape.width),
                            "height": int(shape.height),
                            "filename": getattr(shape.image, "filename", f"slide-{index}-{shape_index}"),
                            "bytes": shape.image.blob,
                        }
                    )

            notes_text = ""
            if getattr(slide, "has_notes_slide", False) and slide.notes_slide.notes_text_frame is not None:
                notes_text = slide.notes_slide.notes_text_frame.text.strip()

            combined_text = "\n".join(part for part in [*texts, notes_text] if part)
            pages.append(
                ParsedSlidePage(
                    page_number=index,
                    text=combined_text,
                    raw_extracted_text=combined_text,
                    structured_text=combined_text,
                    images=images,
                    tables=tables,
                )
            )
            slide_pages_raw.append(
                {
                    "page_number": index,
                    "text_blocks": texts,
                    "notes": notes_text,
                    "tables_count": len(tables),
                    "images_count": len(images),
                }
            )

        return ParsedSlidesDocument(
            pages=pages,
            extracted_tables=[table for page in pages for table in page.tables],
            slide_pages_raw=slide_pages_raw,
        )
