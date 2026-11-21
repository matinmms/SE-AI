from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO, Protocol

from django.core.files.uploadedfile import UploadedFile


@dataclass(frozen=True)
class StoredFile:
    path: str
    url: str | None
    size: int


class StorageError(Exception):
    """Base error for durable media storage operations."""


class StorageNotFoundError(StorageError):
    """The requested object does not exist in the configured backend."""


class StorageProviderError(StorageError):
    """The storage provider could not complete a request."""


class StorageThrottledError(StorageProviderError):
    """The provider is rate limiting us (HTTP 429/503); back off instead of retrying hard."""


class StorageQuotaExceededError(StorageProviderError):
    """The bucket's storage quota is used up; nothing more can be written until space is freed."""


def normalize_storage_path(relative_path: str) -> str:
    """Return a portable, traversal-safe media key without changing DB semantics."""
    normalized = str(relative_path or "").replace("\\", "/").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StorageError("Storage paths must be non-empty relative paths.")
    return path.as_posix()


class StorageServiceProtocol(Protocol):
    def save(self, *, relative_path: str, file_obj: UploadedFile) -> StoredFile: ...

    def save_content(
        self, *, relative_path: str, content: bytes, content_type: str | None = None
    ) -> StoredFile: ...

    def save_file(
        self, *, relative_path: str, local_path: str, content_type: str | None = None
    ) -> StoredFile: ...

    def exists(self, relative_path: str) -> bool: ...

    def open(self, relative_path: str, mode: str = "rb") -> BinaryIO: ...

    def size(self, relative_path: str) -> int: ...

    def path(self, relative_path: str) -> str | None: ...

    def delete(self, relative_path: str) -> None: ...

    def copy(self, *, source_path: str, destination_path: str) -> StoredFile: ...

    def materialize_to_temp(self, relative_path: str) -> AbstractContextManager[str]: ...
