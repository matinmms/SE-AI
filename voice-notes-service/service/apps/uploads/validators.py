from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile

from apps.uploads.exceptions import FileTooLargeException, InvalidFileFormatException, MissingFileException


# Every container ffmpeg/ffprobe can read for speech. The pipeline transcodes whatever arrives
# (video is stripped with -vn), so the list only has to keep out files that are not media at all.
AUDIO_EXTENSIONS = frozenset(
    {
        ".flac", ".m4a", ".mov", ".mp3", ".mp4", ".mpeg", ".mpga", ".ogg", ".wav", ".webm",
        ".aac", ".amr", ".awb", ".3gp", ".3g2", ".opus", ".oga", ".spx", ".wma", ".m4b", ".m4r",
        ".mka", ".aif", ".aiff", ".caf", ".mp2", ".ac3", ".weba", ".m4v", ".mkv", ".avi", ".wmv",
    }
)


def unsupported_format_message(extension: str) -> str:
    shown = extension or "این"
    return (
        f"فرمت فایل ({shown}) پشتیبانی نمی‌شود. فایل را به یکی از فرمت‌های MP3، M4A، WAV، AAC، OGG یا MP4 "
        "تبدیل کن و دوباره بفرست."
    )


@dataclass(frozen=True)
class BaseValidationResult:
    extension: str
    mime_type: str


class BaseFileValidator:
    allowed_extensions: set[str] = set()
    allowed_mime_types: set[str] = set()

    # Whole families the declared type may belong to. The extension is already checked
    # and ffprobe inspects the real content later, so the declared type only has to be
    # plausible; phones label the same .m4a as audio/mp4a-latm, audio/aac or audio/x-m4a.
    allowed_mime_prefixes: tuple[str, ...] = ()

    @property
    def max_size_bytes(self) -> int:
        return 0

    def _mime_allowed(self, mime_type: str) -> bool:
        return mime_type in self.allowed_mime_types or mime_type.startswith(self.allowed_mime_prefixes)

    def check_declared(self, name: str, content_type: str = "") -> tuple[str, str]:
        """Name and declared type only (no content): cheap enough to run on the first chunk."""
        extension = Path(name or "").suffix.lower()
        mime_type = (content_type or "").lower()
        if extension not in self.allowed_extensions:
            # An unfamiliar extension with an audio/video type is still media; ffprobe checks the content.
            media_type = bool(self.allowed_mime_prefixes) and mime_type.startswith(self.allowed_mime_prefixes)
            if not media_type:
                raise InvalidFileFormatException(message=self.unsupported_message(extension))
        if self.allowed_mime_types and mime_type and not self._mime_allowed(mime_type):
            raise InvalidFileFormatException(message=self.unsupported_message(extension))
        return extension, mime_type

    def unsupported_message(self, extension: str) -> str:
        return f"Unsupported file extension: {extension or 'unknown'}."

    def validate(self, file_obj: UploadedFile | None) -> BaseValidationResult:
        if file_obj is None:
            raise MissingFileException()

        extension, mime_type = self.check_declared(file_obj.name, file_obj.content_type or "")

        if file_obj.size > self.max_size_bytes:
            raise FileTooLargeException(
                message=f"Maximum allowed size is {self.max_size_bytes // (1024 * 1024)}MB."
            )

        return BaseValidationResult(extension=extension, mime_type=mime_type or "application/octet-stream")


class AudioFileValidator(BaseFileValidator):
    allowed_mime_prefixes = ("audio/", "video/")
    allowed_extensions = AUDIO_EXTENSIONS

    def unsupported_message(self, extension: str) -> str:
        return unsupported_format_message(extension)

    allowed_mime_types = {
        "audio/flac",
        "audio/mpeg",
        "audio/mp3",
        "audio/ogg",
        "audio/wav",
        "audio/webm",
        "audio/x-wav",
        "audio/mp4",
        "audio/x-m4a",
        "audio/mpga",
        "video/mp4",
        "video/quicktime",
        "video/webm",
        "audio/quicktime",
        "application/octet-stream",
    }
    @property
    def max_size_bytes(self) -> int:
        return settings.AUDIO_MAX_UPLOAD_SIZE_BYTES


class SlidesFileValidator(BaseFileValidator):
    allowed_extensions = {".pdf", ".pptx"}
    allowed_mime_types = {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/octet-stream",
    }
    @property
    def max_size_bytes(self) -> int:
        return settings.SLIDES_MAX_UPLOAD_SIZE_BYTES
