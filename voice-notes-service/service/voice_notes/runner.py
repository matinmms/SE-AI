"""Run ONE voice recording through TesaNote's real pipeline and return the finished note.

    from voice_notes import process_audio
    result = process_audio("lecture-3.m4a", out_dir="out/lecture-3", course_name="Genetics")

What happens inside (all of it is TesaNote's production code, unchanged):
  1. the audio is stored and a Lecture row is created (the pipeline's unit of work);
  2. ffmpeg cleans the audio, it is cut into chunks and transcribed (AvalAI Whisper);
  3. the transcript becomes the "lecturer's words" note (v2.2: the teacher's own words and order,
     nothing added by the AI), rendered to HTML (and optionally PDF).

One call = one voice = one note. The call is synchronous and blocks until the note is ready (a
50-minute lecture is typically a few minutes). Run many calls in parallel from separate worker
PROCESSES to handle many voices at once -- see docs/SCALING.md.
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.service")

import django  # noqa: E402

django.setup()

from django.conf import settings  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.ai_pipeline.exceptions import PipelineError  # noqa: E402
from apps.ai_pipeline.services import LectureProcessingOrchestrator  # noqa: E402
from apps.lectures.models import Lecture, LectureStatus  # noqa: E402
from apps.notes.generation import is_comparison_note_variant  # noqa: E402
from apps.notes.models import GeneratedNote, NoteGenerationStatus  # noqa: E402
from apps.notes.note_styles import STYLE_LECTURER  # noqa: E402
from apps.notes.services import GeneratedNoteService  # noqa: E402
from apps.uploads.models import LectureFile, LectureFileType, UploadStatus  # noqa: E402
from infrastructure.storage.factory import get_storage_service  # noqa: E402

SERVICE_USER_EMAIL = "voice-notes-service@localhost"
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wma", ".mp4", ".webm", ".amr"}


@dataclass
class JobResult:
    job_id: str
    status: str  # "COMPLETED" or "FAILED"
    audio_file: str
    out_dir: str = ""
    note_html: str = ""  # path of the finished note (standalone HTML, open in any browser)
    note_pdf: str = ""  # only when make_pdf=True and Chromium is installed
    note_json: str = ""  # the structured note (blocks) before rendering
    transcript_txt: str = ""
    title: str = ""
    duration_seconds: int = 0
    elapsed_seconds: float = 0.0
    error_stage: str = ""
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _service_user() -> User:
    user = User.objects.filter(email=SERVICE_USER_EMAIL).first()
    if user is None:
        user = User(email=SERVICE_USER_EMAIL, full_name="Voice Notes Service", is_active=True)
        user.set_unusable_password()
        user.save()
    return user


def _probe_duration_seconds(path: Path) -> int:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout.strip()
        return max(1, int(round(float(out))))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not read the audio ({path.name}). Is ffmpeg installed and the file a valid recording?") from exc


def _pick_note(lecture: Lecture) -> GeneratedNote | None:
    notes = [n for n in lecture.generated_notes.all() if not is_comparison_note_variant(n.variant)]
    done = [n for n in notes if n.status == NoteGenerationStatus.COMPLETED]
    return (done or notes or [None])[0]


def process_audio(
    audio_path: str | os.PathLike,
    *,
    out_dir: str | os.PathLike,
    title: str = "",
    course_name: str = "",
    professor_name: str = "",
    make_pdf: bool = False,
    keep_working_files: bool = False,
) -> JobResult:
    """Turn one recording into a v2.2 note. Never raises for pipeline failures: check ``result.status``."""
    started = time.monotonic()
    source = Path(audio_path).expanduser().resolve()
    out = Path(out_dir).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    if not source.is_file():
        raise FileNotFoundError(source)
    if source.suffix.lower() not in AUDIO_SUFFIXES:
        raise ValueError(f"Unsupported audio type {source.suffix!r}. Supported: {sorted(AUDIO_SUFFIXES)}")
    if not settings.AVALAI_API_KEY:
        raise RuntimeError("AVALAI_API_KEY is not set (copy .env.example to .env and fill it in).")

    duration = _probe_duration_seconds(source)
    user = _service_user()
    lecture = Lecture.objects.create(
        user=user,
        title=title or source.stem,
        course_name=course_name or "-",
        professor_name=professor_name,
        duration_seconds=duration,
        status=LectureStatus.UPLOADED,
        # The one switch that makes this the v2.2 "lecturer's words" note.
        processing_metadata={"note_style": STYLE_LECTURER, "source_filename": source.name},
    )
    job_id = str(lecture.id)
    result = JobResult(job_id=job_id, status="FAILED", audio_file=str(source), out_dir=str(out), duration_seconds=duration)
    storage = get_storage_service()
    relative = f"{settings.LECTURE_UPLOAD_ROOT}/{lecture.id}/audio/{uuid.uuid4().hex}{source.suffix.lower()}"
    try:
        stored = storage.save_file(
            relative_path=relative, local_path=str(source),
            content_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
        )
        LectureFile.objects.create(
            lecture=lecture, file_type=LectureFileType.AUDIO, original_name=source.name,
            storage_path=stored.path, mime_type=mimetypes.guess_type(source.name)[0] or "application/octet-stream",
            file_size=source.stat().st_size, duration_seconds=duration, upload_status=UploadStatus.UPLOADED,
            metadata={"duration_seconds": duration},
        )
        orchestrator = LectureProcessingOrchestrator()
        try:
            orchestrator.validate(lecture=lecture)
            orchestrator.start(lecture=lecture, task_id=str(uuid.uuid4()))
            lecture.refresh_from_db()
            orchestrator.run(lecture=lecture)
        except PipelineError as exc:
            orchestrator.fail(lecture=lecture, error=exc)
            result.error_stage, result.error = exc.stage, exc.message
        lecture.refresh_from_db()
        note = _pick_note(lecture)
        if note is None or note.status != NoteGenerationStatus.COMPLETED:
            result.error = result.error or (note.error_message if note else "") or lecture.processing_error or "The note was not produced."
            result.error_stage = result.error_stage or "NOTE_GENERATION"
            return result

        html_relative = GeneratedNoteService.ensure_full_html_artifact(note)
        (out / "note.html").write_bytes(storage.open(html_relative).read())
        result.note_html = str(out / "note.html")
        (out / "note.json").write_text(json.dumps(note.semantic_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result.note_json = str(out / "note.json")
        (out / "transcript.txt").write_text(lecture.merged_transcript or "", encoding="utf-8")
        result.transcript_txt = str(out / "transcript.txt")
        result.title = note.title or lecture.title
        if make_pdf:
            try:
                pdf_relative = GeneratedNoteService.ensure_pdf_artifact(note)
                (out / "note.pdf").write_bytes(storage.open(pdf_relative).read())
                result.note_pdf = str(out / "note.pdf")
            except Exception as exc:  # noqa: BLE001  (the HTML note is still good)
                result.warnings.append(f"PDF was not produced: {exc}")
        result.status = "COMPLETED"
        return result
    except Exception as exc:  # noqa: BLE001
        result.error_stage = result.error_stage or "SERVICE"
        result.error = result.error or f"{type(exc).__name__}: {exc}"
        (out / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        return result
    finally:
        result.elapsed_seconds = round(time.monotonic() - started, 1)
        (out / "result.json").write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        if not keep_working_files:
            shutil.rmtree(Path(settings.MEDIA_ROOT) / settings.LECTURE_UPLOAD_ROOT / str(lecture.id), ignore_errors=True)
