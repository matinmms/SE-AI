from __future__ import annotations

import logging
import os
import random
import time
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import boto3
from boto3.exceptions import S3UploadFailedError
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import UploadedFile

from infrastructure.storage.base import (
    StorageNotFoundError,
    StorageProviderError,
    StorageQuotaExceededError,
    StorageThrottledError,
    StoredFile,
    normalize_storage_path,
)

logger = logging.getLogger("tesanote")


_THROTTLE_ERROR_CODES = {"SlowDown", "TooManyRequests", "RequestLimitExceeded", "ServiceUnavailable"}
_MULTI_DELETE_BATCH = 500
_THROTTLE_ATTEMPTS = 5


def _is_throttle(error: Exception) -> bool:
    if not isinstance(error, ClientError):
        return False
    code = str(error.response.get("Error", {}).get("Code", ""))
    status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
    return code in _THROTTLE_ERROR_CODES or status in (429, 503)


def _retry_after_seconds(error: ClientError, attempt: int) -> float:
    header = error.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("retry-after", "")
    try:
        wait = float(header)
    except (TypeError, ValueError):
        wait = 2.0**attempt
    return min(30.0, wait) + random.uniform(0, 0.5)


_QUOTA_ERROR_CODES = {"QuotaExceeded", "StorageQuotaExceeded", "QuotaExceededException", "InsufficientStorage"}

# boto3's managed uploads raise S3UploadFailedError (not a ClientError) when a part fails, so it must be
# caught with the other transport errors or it escapes as an "unexpected" failure.
_UPLOAD_ERRORS = (BotoCoreError, ClientError, S3UploadFailedError)
_MB = 1024 * 1024
# The provider's CDN intermittently refuses multipart parts (HTTP 503, or a 400 with a malformed
# response), while a single PUT keeps working. Files up to this size are sent as one PUT; a failed
# attempt is retried once with the other method.
SINGLE_PUT_MAX_BYTES = 256 * _MB
_SINGLE_PUT = TransferConfig(multipart_threshold=SINGLE_PUT_MAX_BYTES + 1, use_threads=False)
_MULTIPART = TransferConfig(multipart_threshold=8 * _MB, multipart_chunksize=8 * _MB, max_concurrency=2)


class _KeepOpen:
    """boto3 closes the stream it uploads, even when the upload fails, which made the second attempt
    die with "seek of closed file". This hands boto3 a stream whose close() does nothing."""

    def __init__(self, stream) -> None:
        self._stream = stream

    def __getattr__(self, name):
        return getattr(self._stream, name)

    def close(self) -> None:  # noqa: D401 - intentionally a no-op
        return None


def _is_clock_skew(error: Exception) -> bool:
    """The provider refuses signatures from a server whose clock is off by minutes. Retrying
    cannot help until the server clock is fixed, so this is reported loudly instead."""
    if isinstance(error, ClientError):
        return str(error.response.get("Error", {}).get("Code", "")) == "RequestTimeTooSkewed"
    return "RequestTimeTooSkewed" in str(error)


def _transfer_attempts(size: int | None) -> tuple[TransferConfig, ...]:
    if size is not None and size > SINGLE_PUT_MAX_BYTES:
        return (_MULTIPART, _MULTIPART, _MULTIPART)
    return (_SINGLE_PUT, _MULTIPART, _SINGLE_PUT)


def _upload_error(error: Exception) -> StorageProviderError:
    """A write failure; a full bucket gets its own type because retrying cannot help."""
    if isinstance(error, S3UploadFailedError):
        text = str(error)
        if any(code in text for code in _QUOTA_ERROR_CODES) or "(507)" in text:
            from infrastructure.storage.quota_state import mark_storage_full

            mark_storage_full()
            return StorageQuotaExceededError("Object storage quota exceeded.")
    if isinstance(error, ClientError):
        code = str(error.response.get("Error", {}).get("Code", ""))
        status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
        if code in _QUOTA_ERROR_CODES or status == 507:
            from infrastructure.storage.quota_state import mark_storage_full

            mark_storage_full()
            return StorageQuotaExceededError("Object storage quota exceeded.")
    return StorageProviderError("Object storage upload failed.")


@dataclass(frozen=True)
class ObjectMetadata:
    size: int
    content_type: str
    etag: str


class S3CompatibleObjectStorageService:
    """Private S3-compatible storage. Database paths remain portable object keys."""

    def __init__(self, *, client=None) -> None:
        self.bucket = settings.OBJECT_STORAGE_BUCKET
        self.key_prefix = settings.OBJECT_STORAGE_KEY_PREFIX
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.OBJECT_STORAGE_ENDPOINT,
            region_name=settings.OBJECT_STORAGE_REGION,
            aws_access_key_id=settings.OBJECT_STORAGE_ACCESS_KEY,
            aws_secret_access_key=settings.OBJECT_STORAGE_SECRET_KEY,
            verify=settings.OBJECT_STORAGE_VERIFY_TLS,
            config=Config(
                signature_version=settings.OBJECT_STORAGE_SIGNATURE_VERSION,
                connect_timeout=settings.OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS,
                read_timeout=settings.OBJECT_STORAGE_READ_TIMEOUT_SECONDS,
                retries={"max_attempts": 3, "mode": "standard"},
                s3={"addressing_style": settings.OBJECT_STORAGE_ADDRESSING_STYLE},
            ),
        )

    @staticmethod
    def _content_type(file_obj, fallback: str | None = None) -> str | None:
        return fallback or getattr(file_obj, "content_type", None)

    @staticmethod
    def _is_missing(error: ClientError) -> bool:
        code = str(error.response.get("Error", {}).get("Code", ""))
        return code in {"404", "NoSuchKey", "NotFound", "NoSuchObject"}

    def _extra_args(self, content_type: str | None) -> dict[str, str]:
        return {"ContentType": content_type} if content_type else {}

    def _remote_key(self, relative_path: str) -> str:
        key = normalize_storage_path(relative_path)
        return f"{self.key_prefix}/{key}" if self.key_prefix else key

    def _head(self, key: str) -> dict:
        remote_key = self._remote_key(key)
        try:
            return self.client.head_object(Bucket=self.bucket, Key=remote_key)
        except ClientError as error:
            if self._is_missing(error):
                raise StorageNotFoundError("Object does not exist.") from error
            raise StorageProviderError("Object storage metadata lookup failed.") from error
        except BotoCoreError as error:
            raise StorageProviderError("Object storage metadata lookup failed.") from error

    def head_metadata(self, relative_path: str) -> ObjectMetadata:
        """Return only the object facts needed by direct-upload finalization."""
        payload = self._head(normalize_storage_path(relative_path))
        return ObjectMetadata(
            size=int(payload.get("ContentLength", 0) or 0),
            content_type=str(payload.get("ContentType", "") or ""),
            etag=str(payload.get("ETag", "") or "").strip('"'),
        )

    def generate_presigned_put(
        self,
        *,
        relative_path: str,
        content_type: str,
        expires_in_seconds: int,
    ) -> str:
        """Create a short-lived PUT authorization for one server-generated key."""
        key = normalize_storage_path(relative_path)
        try:
            return str(
                self.client.generate_presigned_url(
                    "put_object",
                    Params={
                        "Bucket": self.bucket,
                        "Key": self._remote_key(key),
                        "ContentType": content_type,
                    },
                    ExpiresIn=max(1, int(expires_in_seconds)),
                    HttpMethod="PUT",
                )
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageProviderError("Object storage upload authorization failed.") from error

    def generate_presigned_get(
        self,
        *,
        relative_path: str,
        expires_in_seconds: int,
        content_type: str | None = None,
    ) -> str:
        """Create a short-lived GET authorization for one private object (the bucket stays private)."""
        key = normalize_storage_path(relative_path)
        params = {"Bucket": self.bucket, "Key": self._remote_key(key)}
        if content_type:
            params["ResponseContentType"] = content_type
        try:
            return str(
                self.client.generate_presigned_url(
                    "get_object",
                    Params=params,
                    ExpiresIn=max(1, int(expires_in_seconds)),
                    HttpMethod="GET",
                )
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageProviderError("Object storage download authorization failed.") from error

    def save(self, *, relative_path: str, file_obj: UploadedFile) -> StoredFile:
        key = normalize_storage_path(relative_path)
        remote_key = self._remote_key(key)
        try:
            last_error: Exception | None = None
            for attempt, config in enumerate(_transfer_attempts(getattr(file_obj, "size", None))):
                try:
                    if hasattr(file_obj, "seek"):
                        file_obj.seek(0)
                    self.client.upload_fileobj(
                        _KeepOpen(file_obj),
                        self.bucket,
                        remote_key,
                        ExtraArgs=self._extra_args(self._content_type(file_obj)),
                        Config=config,
                    )
                    last_error = None
                    break
                except _UPLOAD_ERRORS as error:
                    last_error = error
                    if _is_clock_skew(error):
                        logger.error(
                            "object_storage_clock_skew",
                            extra={"object_key": key, "error_detail": str(error)[:300]},
                        )
                        break
                    if isinstance(_upload_error(error), StorageQuotaExceededError):
                        break  # a full bucket cannot be helped by retrying
                    logger.warning(
                        "object_storage_upload_attempt_failed",
                        extra={"object_key": key, "attempt": attempt + 1, "error_type": type(error).__name__, "error_detail": str(error)[:400]},
                    )
                    time.sleep(min(2.0 * (attempt + 1), 6.0))
            if last_error is not None:
                raise last_error
            size = int(self._head(key).get("ContentLength", 0))
        except _UPLOAD_ERRORS as error:
            raise _upload_error(error) from error
        logger.info("object_storage_write_completed", extra={"object_key": key, "size_bytes": size})
        return StoredFile(path=key, url=None, size=size)

    def save_content(
        self, *, relative_path: str, content: bytes, content_type: str | None = None
    ) -> StoredFile:
        content_file = ContentFile(content)
        if content_type:
            content_file.content_type = content_type
        return self.save(relative_path=relative_path, file_obj=content_file)

    def save_file(
        self, *, relative_path: str, local_path: str, content_type: str | None = None
    ) -> StoredFile:
        key = normalize_storage_path(relative_path)
        remote_key = self._remote_key(key)
        try:
            last_error: Exception | None = None
            for attempt, config in enumerate(_transfer_attempts(os.path.getsize(local_path))):
                try:
                    self.client.upload_file(
                        local_path,
                        self.bucket,
                        remote_key,
                        ExtraArgs=self._extra_args(content_type),
                        Config=config,
                    )
                    last_error = None
                    break
                except _UPLOAD_ERRORS as error:
                    last_error = error
                    if _is_clock_skew(error):
                        logger.error(
                            "object_storage_clock_skew",
                            extra={"object_key": key, "error_detail": str(error)[:300]},
                        )
                        break
                    if isinstance(_upload_error(error), StorageQuotaExceededError):
                        break
                    logger.warning(
                        "object_storage_upload_attempt_failed",
                        extra={"object_key": key, "attempt": attempt + 1, "error_type": type(error).__name__, "error_detail": str(error)[:400]},
                    )
                    time.sleep(min(2.0 * (attempt + 1), 6.0))
            if last_error is not None:
                raise last_error
            size = int(self._head(key).get("ContentLength", 0))
        except _UPLOAD_ERRORS as error:
            raise _upload_error(error) from error
        logger.info("object_storage_write_completed", extra={"object_key": key, "size_bytes": size})
        return StoredFile(path=key, url=None, size=size)

    def exists(self, relative_path: str) -> bool:
        try:
            self._head(normalize_storage_path(relative_path))
            return True
        except StorageNotFoundError:
            return False

    def open(self, relative_path: str, mode: str = "rb") -> BinaryIO:
        if mode != "rb":
            raise StorageProviderError("Object storage supports binary reads only.")
        key = normalize_storage_path(relative_path)
        remote_key = self._remote_key(key)
        try:
            return self.client.get_object(Bucket=self.bucket, Key=remote_key)["Body"]
        except ClientError as error:
            if self._is_missing(error):
                raise StorageNotFoundError("Object does not exist.") from error
            raise StorageProviderError("Object storage download failed.") from error
        except BotoCoreError as error:
            raise StorageProviderError("Object storage download failed.") from error

    def size(self, relative_path: str) -> int:
        return int(self._head(normalize_storage_path(relative_path)).get("ContentLength", 0))

    def path(self, relative_path: str) -> str | None:
        return None

    def delete_many(self, relative_paths: list[str]) -> dict[str, str]:
        """Delete many objects, one request per 500 keys. Returns path -> "deleted" | "failed" | "throttled".

        Deleting one by one meant two requests per object (existence check, then delete); a
        500-object cleanup batch tripped the provider's rate limit (HTTP 429) and every delete
        after the first batch failed. A missing key counts as deleted. When the provider keeps
        throttling, the remaining paths are reported "throttled" so the caller stops and retries
        later instead of hammering.
        """
        keys = [normalize_storage_path(path) for path in relative_paths]
        outcome: dict[str, str] = {}
        for start in range(0, len(keys), _MULTI_DELETE_BATCH):
            part = keys[start : start + _MULTI_DELETE_BATCH]
            remote_by_key = {self._remote_key(key): key for key in part}
            try:
                response = self._with_backoff(
                    lambda: self.client.delete_objects(
                        Bucket=self.bucket,
                        Delete={"Objects": [{"Key": remote} for remote in remote_by_key], "Quiet": False},
                    )
                )
            except StorageThrottledError:
                for key in keys[start:]:
                    outcome[key] = "throttled"
                return outcome
            except (BotoCoreError, ClientError):
                for key in part:
                    outcome[key] = "failed"
                continue
            refused = {item.get("Key") for item in response.get("Errors", [])}
            for remote, key in remote_by_key.items():
                outcome[key] = "failed" if remote in refused else "deleted"
        return outcome

    def purge_noncurrent_versions(
        self, *, relative_prefix: str, older_than, max_seconds: float = 240.0
    ) -> dict[str, int]:
        """Permanently remove old versions and delete markers under one prefix.

        The bucket has versioning enabled, so a normal delete only hides an object behind a
        delete marker; the old bytes stay and keep counting toward the storage quota. This
        removes what deletion left behind -- noncurrent versions and delete markers older than
        `older_than` -- and never touches the current (live) version of any object. It is only
        ever pointed at the temporary/ hierarchy, whose files are disposable by design.
        """
        prefix = self._remote_key(relative_prefix.rstrip("/")) + "/"
        result = {"versions": 0, "markers": 0, "bytes": 0, "failed": 0, "throttled": 0, "complete": 0}
        deadline = time.monotonic() + max_seconds
        kwargs = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
        while True:
            try:
                page = self._with_backoff(lambda: self.client.list_object_versions(**kwargs))
            except StorageThrottledError:
                result["throttled"] = 1
                return result
            except (BotoCoreError, ClientError) as error:
                raise StorageProviderError("Object storage version listing failed.") from error
            doomed: list[dict] = []
            sizes: dict[tuple[str, str], int] = {}
            for version in page.get("Versions", []):
                if not version.get("IsLatest") and version["LastModified"] < older_than:
                    doomed.append({"Key": version["Key"], "VersionId": version["VersionId"]})
                    sizes[(version["Key"], version["VersionId"])] = int(version.get("Size", 0))
            for marker in page.get("DeleteMarkers", []):
                if marker["LastModified"] < older_than:
                    doomed.append({"Key": marker["Key"], "VersionId": marker["VersionId"]})
            for start in range(0, len(doomed), _MULTI_DELETE_BATCH):
                part = doomed[start : start + _MULTI_DELETE_BATCH]
                try:
                    response = self._with_backoff(
                        lambda: self.client.delete_objects(
                            Bucket=self.bucket, Delete={"Objects": part, "Quiet": False}
                        )
                    )
                except StorageThrottledError:
                    result["throttled"] = 1
                    return result
                except (BotoCoreError, ClientError):
                    result["failed"] += len(part)
                    continue
                for item in response.get("Deleted", []):
                    key_version = (item.get("Key"), item.get("VersionId"))
                    if key_version in sizes:
                        result["versions"] += 1
                        result["bytes"] += sizes[key_version]
                    else:
                        result["markers"] += 1
                result["failed"] += len(response.get("Errors", []))
            if not page.get("IsTruncated"):
                result["complete"] = 1
                return result
            if time.monotonic() >= deadline:
                return result
            kwargs["KeyMarker"] = page.get("NextKeyMarker")
            kwargs["VersionIdMarker"] = page.get("NextVersionIdMarker")
            time.sleep(0.3)

    def _with_backoff(self, call):
        for attempt in range(1, _THROTTLE_ATTEMPTS + 1):
            try:
                return call()
            except ClientError as error:
                if not _is_throttle(error):
                    raise
                if attempt == _THROTTLE_ATTEMPTS:
                    raise StorageThrottledError("Object storage is rate limiting requests.") from error
                time.sleep(_retry_after_seconds(error, attempt))
        raise AssertionError("unreachable")

    def delete(self, relative_path: str) -> None:
        key = normalize_storage_path(relative_path)
        remote_key = self._remote_key(key)
        try:
            self.client.delete_object(Bucket=self.bucket, Key=remote_key)
        except (BotoCoreError, ClientError) as error:
            raise StorageProviderError("Object storage deletion failed.") from error

    def copy(self, *, source_path: str, destination_path: str) -> StoredFile:
        source_key = normalize_storage_path(source_path)
        destination_key = normalize_storage_path(destination_path)
        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=self._remote_key(destination_key),
                CopySource={"Bucket": self.bucket, "Key": self._remote_key(source_key)},
            )
            size = int(self._head(destination_key).get("ContentLength", 0))
        except (BotoCoreError, ClientError) as error:
            raise StorageProviderError("Object storage copy failed.") from error
        return StoredFile(path=destination_key, url=None, size=size)

    @contextmanager
    def materialize_to_temp(self, relative_path: str) -> Iterator[str]:
        suffix = Path(relative_path).suffix
        descriptor, temporary_path = tempfile.mkstemp(prefix="tesanote-object-", suffix=suffix)
        try:
            with (
                os.fdopen(descriptor, "wb") as destination,
                self.open(relative_path, "rb") as source,
            ):
                shutil.copyfileobj(source, destination, length=1024 * 1024)
            yield temporary_path
        finally:
            Path(temporary_path).unlink(missing_ok=True)
