from __future__ import annotations

from pathlib import Path

try:
    import camelot  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    camelot = None

try:
    import tabula  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover
    tabula = None


class TableExtractor:
    def extract(self, *, file_path: str | None, page_number: int) -> list[dict[str, object]]:
        if not file_path:
            return []

        tables: list[dict[str, object]] = []
        if camelot is not None:
            try:
                result = camelot.read_pdf(file_path, pages=str(page_number))
                for table in result:
                    tables.append({"page": page_number, "data": table.df.values.tolist()})
                if tables:
                    return tables
            except Exception:
                pass

        if tabula is not None:
            try:
                result = tabula.read_pdf(file_path, pages=page_number, multiple_tables=True)
                for table in result or []:
                    tables.append({"page": page_number, "data": table.fillna("").values.tolist()})
            except Exception:
                pass

        return tables
