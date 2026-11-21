from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from infrastructure.storage.base import StoredFile

logger = logging.getLogger("tesanote")


class HybridStorageService:
    """Object-first reads with local fallback only for confirmed missing objects."""

    def __init__(self, *, object_storage, local_storage) -> None:
        self.object_storage = object_storage
        self.local_storage = local_storage

    def save(self, **kwargs) -> StoredFile:
        return self.object_storage.save(**kwargs)

    def save_content(self, **kwargs) -> StoredFile:
        return self.object_storage.save_content(**kwargs)

    def save_file(self, **kwargs) -> StoredFile:
        return self.object_storage.save_file(**kwargs)

    def _object_exists(self, relative_path: str) -> bool:
        # Provider failures deliberately propagate; they are not a local miss.
        return self.object_storage.exists(relative_path)

    def exists(self, relative_path: str) -> bool:
        if self._object_exists(relative_path):
            return True
        return self.local_storage.exists(relative_path)

    def open(self, relative_path: str, mode: str = "rb"):
        if self._object_exists(relative_path):
            return self.object_storage.open(relative_path, mode)
        logger.info("object_storage_local_fallback_read", extra={"object_key": relative_path})
        return self.local_storage.open(relative_path, mode)

    def size(self, relative_path: str) -> int:
        if self._object_exists(relative_path):
            return self.object_storage.size(relative_path)
        return self.local_storage.size(relative_path)

    def path(self, relative_path: str) -> str | None:
        if self._object_exists(relative_path):
            return None
        return self.local_storage.path(relative_path)

    def delete(self, relative_path: str) -> None:
        if self._object_exists(relative_path):
            self.object_storage.delete(relative_path)
        self.local_storage.delete(relative_path)

    def purge_noncurrent_versions(self, **kwargs) -> dict[str, int]:
        return self.object_storage.purge_noncurrent_versions(**kwargs)

    def delete_many(self, relative_paths: list[str]) -> dict[str, str]:
        """Batch delete: one provider request per 500 objects, no per-object existence check."""
        outcome = self.object_storage.delete_many(relative_paths)
        for path, state in outcome.items():
            if state == "deleted":
                self.local_storage.delete(path)
        return outcome

    def copy(self, *, source_path: str, destination_path: str) -> StoredFile:
        if self._object_exists(source_path):
            return self.object_storage.copy(
                source_path=source_path, destination_path=destination_path
            )
        with self.local_storage.open(source_path, "rb") as source:
            return self.object_storage.save(relative_path=destination_path, file_obj=source)

    @contextmanager
    def materialize_to_temp(self, relative_path: str) -> Iterator[str]:
        if self._object_exists(relative_path):
            with self.object_storage.materialize_to_temp(relative_path) as local_path:
                yield local_path
            return
        logger.info(
            "object_storage_local_fallback_materialize", extra={"object_key": relative_path}
        )
        with self.local_storage.materialize_to_temp(relative_path) as local_path:
            yield local_path
