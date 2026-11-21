from __future__ import annotations

from pathlib import Path


def temporary_artifact_path(*, user_id: str, lecture_id: str, category: str, filename: str) -> str:
    """Build the dedicated prefix targeted by the provider lifecycle rule."""
    extension = Path(filename).suffix.lower()
    safe_stem = Path(filename).stem.replace(" ", "_") or "artifact"
    safe_category = category.strip("/")
    return (
        f"temporary/users/{user_id}/lectures/{lecture_id}/"
        f"{safe_category}/{safe_stem}{extension}"
    )
