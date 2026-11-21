"""Offline checks. Makes NO provider calls and costs nothing."""
from __future__ import annotations

import shutil


def run() -> int:
    from .runner import settings  # sets up Django

    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        print(f"[{'OK' if passed else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")

    check("AVALAI_API_KEY is set", bool(settings.AVALAI_API_KEY), "put it in .env" if not settings.AVALAI_API_KEY else "")
    for tool in ("ffmpeg", "ffprobe"):
        check(f"{tool} installed", shutil.which(tool) is not None)
    try:
        from django.db import connection

        connection.ensure_connection()
        from django.db.migrations.executor import MigrationExecutor

        pending = MigrationExecutor(connection).migration_plan(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
        check("database reachable and migrated", not pending, "run: python manage.py migrate" if pending else "")
    except Exception as exc:  # noqa: BLE001
        check("database reachable", False, str(exc))
    try:
        from apps.notes.template_engine import NoteTemplateEngine

        html = NoteTemplateEngine().render(body_content="<div class='template-wrapper'>x</div>")
        check("note template and fonts render", "x" in html and len(html) > 10_000)
    except Exception as exc:  # noqa: BLE001
        check("note template and fonts render", False, str(exc))
    check("v2.2 switches on", bool(settings.NOTE_STYLE_CHOICE_ENABLED and settings.NOTE_QUALITY_V42_ENABLED and settings.NOTE_QUALITY_V21_ENABLED))
    print("\nModels in use (from code defaults / .env):")
    print(f"  speech-to-text : {settings.TRANSCRIPTION_PRIMARY_PROVIDER} / {settings.AVALAI_STT_MODEL}")
    print(f"  note (v2)      : {settings.NOTE_PRIMARY_PROVIDER} / {getattr(settings, 'NOTE_GENERATION_V2_MODEL', '')}")
    print(f"  AvalAI limits  : {settings.AVALAI_RPM_LIMIT} req/min, {settings.AVALAI_TPM_LIMIT} tokens/min, "
          f"{settings.AVALAI_PROVIDER_MAX_CONCURRENCY} concurrent")
    print(f"  PDF (optional) : node={shutil.which('node') or 'missing'}  chromium={'found' if shutil.which('chromium') or shutil.which('chromium-browser') or shutil.which('google-chrome') else 'missing'}")
    print("\nSELFTEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1
