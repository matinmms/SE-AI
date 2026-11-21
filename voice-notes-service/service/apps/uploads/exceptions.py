from __future__ import annotations

from rest_framework import status
from rest_framework.exceptions import APIException


class UploadAPIException(APIException):
    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = "Upload request failed."
    default_code = "UPLOAD_ERROR"

    def __init__(
        self,
        *,
        message: str,
        error_code: str,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        details: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = {
            "success": False,
            "error_code": error_code,
            "message": message,
            "details": details or {},
        }


class MissingFileException(UploadAPIException):
    def __init__(self) -> None:
        super().__init__(message="No file was provided.", error_code="MISSING_FILE")


class InvalidFileFormatException(UploadAPIException):
    def __init__(self, *, message: str = "Invalid file format.") -> None:
        super().__init__(message=message, error_code="INVALID_FILE_FORMAT")


class FileTooLargeException(UploadAPIException):
    def __init__(self, *, message: str) -> None:
        super().__init__(message=message, error_code="FILE_TOO_LARGE")


class InvalidDurationException(UploadAPIException):
    def __init__(self, *, message: str) -> None:
        super().__init__(message=message, error_code="INVALID_DURATION")


class CorruptedFileException(UploadAPIException):
    def __init__(self, *, message: str) -> None:
        super().__init__(message=message, error_code="CORRUPTED_FILE")


class SubscriptionLimitExceededException(UploadAPIException):
    def __init__(self, *, available_minutes: int, required_minutes: int, message: str) -> None:
        super().__init__(
            message=message,
            error_code="INSUFFICIENT_REMAINING_MINUTES",
            status_code=status.HTTP_403_FORBIDDEN,
            details={
                "allowed": False,
                "available_minutes": available_minutes,
                "required_minutes": required_minutes,
                "message": message,
            },
        )


class WalletInsufficientException(UploadAPIException):
    """A wallet user's balance does not cover the approximate price of this file."""

    def __init__(self, *, available_toman: int, estimate_toman: int, required_minutes: int, message: str) -> None:
        super().__init__(
            message=message,
            error_code="INSUFFICIENT_WALLET_BALANCE",
            status_code=status.HTTP_403_FORBIDDEN,
            details={
                "allowed": False,
                "wallet": True,
                "available_toman": available_toman,
                "estimate_toman": estimate_toman,
                "required_minutes": required_minutes,
                "message": message,
            },
        )
