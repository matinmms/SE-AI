from django.conf import settings

from infrastructure.storage.hybrid import HybridStorageService
from infrastructure.storage.local import LocalStorageService
from infrastructure.storage.s3 import S3CompatibleObjectStorageService


def get_local_storage_service() -> LocalStorageService:
    return LocalStorageService()


def get_object_storage_service() -> S3CompatibleObjectStorageService:
    return S3CompatibleObjectStorageService()


def get_storage_service():
    mode = settings.STORAGE_MODE
    if mode == "local":
        return get_local_storage_service()
    if mode == "object":
        return get_object_storage_service()
    if mode == "hybrid":
        return HybridStorageService(
            object_storage=get_object_storage_service(),
            local_storage=get_local_storage_service(),
        )
    raise RuntimeError(f"Unsupported STORAGE_MODE: {mode}")
