from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from django.core.files.base import ContentFile, File
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import UploadedFile

from infrastructure.storage.base import StoredFile, normalize_storage_path


class LocalStorageService:
    def save(self, *, relative_path: str, file_obj: UploadedFile) -> StoredFile:
        stored_path = default_storage.save(normalize_storage_path(relative_path), file_obj)
        url = default_storage.url(stored_path) if hasattr(default_storage, "url") else None
        return StoredFile(path=stored_path, url=url, size=file_obj.size)

    def save_content(
        self, *, relative_path: str, content: bytes, content_type: str | None = None
    ) -> StoredFile:
        content_file = ContentFile(content)
        if content_type:
            content_file.content_type = content_type
        stored_path = default_storage.save(normalize_storage_path(relative_path), content_file)
        url = default_storage.url(stored_path) if hasattr(default_storage, "url") else None
        return StoredFile(path=stored_path, url=url, size=len(content))

    def save_file(
        self, *, relative_path: str, local_path: str, content_type: str | None = None
    ) -> StoredFile:
        with Path(local_path).open("rb") as handle:
            file_obj = File(handle, name=Path(local_path).name)
            if content_type:
                file_obj.content_type = content_type
            stored_path = default_storage.save(normalize_storage_path(relative_path), file_obj)
        url = default_storage.url(stored_path) if hasattr(default_storage, "url") else None
        return StoredFile(path=stored_path, url=url, size=Path(local_path).stat().st_size)

    def exists(self, relative_path: str) -> bool:
        return default_storage.exists(normalize_storage_path(relative_path))

    def open(self, relative_path: str, mode: str = "rb") -> BinaryIO:
        return default_storage.open(normalize_storage_path(relative_path), mode)

    def size(self, relative_path: str) -> int:
        return int(default_storage.size(normalize_storage_path(relative_path)))

    def path(self, relative_path: str) -> str | None:
        if hasattr(default_storage, "path"):
            return default_storage.path(normalize_storage_path(relative_path))
        return None

    def delete(self, relative_path: str) -> None:
        normalized = normalize_storage_path(relative_path)
        if default_storage.exists(normalized):
            default_storage.delete(normalized)

    def copy(self, *, source_path: str, destination_path: str) -> StoredFile:
        with self.open(source_path, "rb") as source:
            stored_path = default_storage.save(
                normalize_storage_path(destination_path), File(source)
            )
        url = default_storage.url(stored_path) if hasattr(default_storage, "url") else None
        return StoredFile(path=stored_path, url=url, size=self.size(stored_path))

    @contextmanager
    def materialize_to_temp(self, relative_path: str) -> Iterator[str]:
        local_path = self.path(relative_path)
        if local_path:
            yield local_path
            return
        suffix = Path(relative_path).suffix
        descriptor, temporary_path = tempfile.mkstemp(prefix="tesanote-media-", suffix=suffix)
        try:
            with (
                os.fdopen(descriptor, "wb") as destination,
                self.open(relative_path, "rb") as source,
            ):
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            yield temporary_path
        finally:
            Path(temporary_path).unlink(missing_ok=True)
