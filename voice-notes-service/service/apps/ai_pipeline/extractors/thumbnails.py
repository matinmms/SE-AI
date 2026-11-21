from __future__ import annotations

from pathlib import Path

from django.core.files.base import ContentFile

from apps.storage_lifecycle.services import StorageArtifactLifecycleService
from infrastructure.storage.factory import get_storage_service


class ThumbnailGenerator:
    def save_pdf_thumbnails(self, *, lecture, thumbnails: list[tuple[int, bytes]]) -> dict[int, str]:
        storage = get_storage_service()
        output: dict[int, str] = {}
        for page_number, image_bytes in thumbnails:
            path = f"lectures/{lecture.id}/slides/thumbnails/page-{page_number}.png"
            stored = storage.save(relative_path=path, file_obj=ContentFile(image_bytes, name=Path(path).name))
            StorageArtifactLifecycleService.register_persistent(
                user_id=str(lecture.user_id),
                lecture_id=str(lecture.id),
                storage_path=stored.path,
                size_bytes=stored.size,
                artifact_type="slide_thumbnail",
            )
            output[page_number] = stored.path
        return output

    def create_placeholder_thumbnails(self, *, lecture, page_numbers: list[int]) -> dict[int, str]:
        storage = get_storage_service()
        output: dict[int, str] = {}
        for page_number in page_numbers:
            svg_markup = f"""
            <svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180" fill="none">
              <rect width="320" height="180" rx="24" fill="#F8FAFC"/>
              <rect x="36" y="18" width="114" height="146" rx="16" transform="rotate(-6 36 18)" fill="#EEF2FF" stroke="#A5B4FC"/>
              <rect x="106" y="16" width="126" height="150" rx="18" fill="white" stroke="#818CF8"/>
              <rect x="182" y="24" width="108" height="140" rx="16" transform="rotate(5 182 24)" fill="#E0E7FF" stroke="#93C5FD"/>
              <rect x="126" y="48" width="84" height="10" rx="5" fill="#C7D2FE"/>
              <rect x="126" y="70" width="68" height="7" rx="3.5" fill="#DBEAFE"/>
              <rect x="126" y="84" width="76" height="7" rx="3.5" fill="#DBEAFE"/>
              <rect x="126" y="98" width="56" height="7" rx="3.5" fill="#DBEAFE"/>
              <text x="126" y="130" fill="#4F46E5" font-size="18" font-family="Arial, sans-serif" font-weight="700">Slide {page_number}</text>
            </svg>
            """.strip()
            path = f"lectures/{lecture.id}/slides/thumbnails/page-{page_number}.svg"
            stored = storage.save(
                relative_path=path,
                file_obj=ContentFile(svg_markup.encode("utf-8"), name=Path(path).name),
            )
            StorageArtifactLifecycleService.register_persistent(
                user_id=str(lecture.user_id),
                lecture_id=str(lecture.id),
                storage_path=stored.path,
                size_bytes=stored.size,
                artifact_type="slide_thumbnail",
            )
            output[page_number] = stored.path
        return output
