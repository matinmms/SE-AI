# Voice → Note service (v2.2, "the lecturer's words")

> **خلاصه (فارسی):** این پوشه فقط «سرویس تبدیل یک ویس به جزوه» تسانت است — همان کد پروداکشن، با نسخهٔ جزوهٔ ۲٫۲ (دقیقاً حرف استاد، بدون اضافه‌کردن چیزی توسط هوش مصنوعی). مدل‌ها و تنظیمات از قبل ست شده‌اند؛ فقط کلید `AVALAI_API_KEY` را در `.env` بگذارید. کار شما: اجرای چند ویس هم‌زمان (۱۰ تا ۲۰ تا)، صف و هم‌زمانی، دریافت ورودی و تحویل خروجی، و یک فرانت ساده. راهنماها: `docs/SCALING.md` و `docs/FRONTEND_GUIDE.md`.

## What this is

TesaNote turns a student's class recording into a study note. This folder is **only that one step**, cut out of the main product so it can run on its own server:

```
one audio file ──►  clean audio (ffmpeg) ─► split into chunks ─► speech-to-text (AvalAI Whisper)
                 ─► note writer (v2.2) ─► finished note (HTML, optional PDF) + transcript + structured JSON
```

* **Version:** v2.2 = the *lecturer's own words and order*; the AI adds nothing of its own. (TesaNote's other style, "v4.2 explained", is **not** part of this service.)
* **Code:** the real production pipeline, copied unchanged (`service/apps`, `service/services`, `service/infrastructure`). Prompts, model choices, chunking, retries, term protection and quality guards are all inside it. Do not rewrite it — wrap it.
* **No front end, no accounts, no billing, no HTTP API.** Those are *yours* to build around the service (see below).

## What you build (the actual task)

Today the product handles **one voice → one note**, one after another. The goal of your project is to let a student give **10–20 voices at once** (dragged in one by one, or a whole folder) and have **all of them processed at the same time**, each producing its own note (or the voices becoming one set of sources — your call, see "Sources vs. separate notes" in `docs/SCALING.md`).

So **you own**:

1. **Intake** – how voices arrive (upload endpoint, folder watch, S3 bucket, …).
2. **Concurrency & queueing** – run many jobs in parallel, fairly, without overloading the box or AvalAI; several students at the same time.
3. **Delivery** – where finished notes go and how the student gets them.
4. **A small front end** – upload many files, see per-file progress, download results (`docs/FRONTEND_GUIDE.md` has a ready brief).

**You do not need to touch** the pipeline internals.

## The one function you call

```python
from voice_notes import process_audio

result = process_audio(
    "lecture-3.m4a",
    out_dir="out/lecture-3",        # note.html, note.json, transcript.txt, result.json (+ note.pdf)
    title="Lecture 3",              # optional
    course_name="Genetics",         # optional
    professor_name="Dr. X",         # optional
    make_pdf=False,                 # True needs Chromium (see Docker)
)
result.status            # "COMPLETED" | "FAILED"
result.note_html         # path of the finished note
result.error_stage       # when FAILED: where it broke (e.g. TRANSCRIPTION, NOTE_GENERATION)
result.error             # when FAILED: why
```

* It is **synchronous**: it returns when the note is done (minutes for a short recording; longer lectures take proportionally longer — measure with your own files).
* It **never raises for pipeline failures** — check `result.status`. (It raises only for bad input: missing file, unsupported type, no API key.)
* It is safe to call **from several processes at once**. Do not call it from several *threads* of one process; use processes (see `docs/SCALING.md`).
* CLI equivalent: `python -m voice_notes process lecture.m4a --out out/lecture --course "Genetics"`

Supported input: mp3, wav, m4a, aac, ogg, opus, flac, wma, mp4, webm, amr. Maximum length per file: **150 minutes** (`MAX_AUDIO_DURATION_MINUTES`).

## Setup (≈10 minutes)

Requirements: Python 3.12, **ffmpeg** (with ffprobe), and for PDF only: Node 20 + Chromium + Playwright (the Dockerfile has all of it).

```bash
cd voice-notes-service
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -c constraints.txt

cp .env.example .env            # then put AVALAI_API_KEY=... in .env  (the ONLY required value)

cd service
python manage.py migrate        # creates the local SQLite database (service/data/)
python -m voice_notes selftest  # offline checks, NO provider calls, no cost
python -m voice_notes process /path/to/short-lecture.m4a --out ../out/test --title "Test"
```

`selftest` prints which models are in use. Open `out/test/note.html` in a browser to see the result. **Test with a short (2–5 min) recording first** — every run is billed by AvalAI.

Docker: `docker build -t voice-notes .` (includes ffmpeg, Chromium, Playwright).

## Folder map

```
.env.example            the only config file you edit (copy to .env)
requirements.txt        Python dependencies (+ constraints.txt: exact pinned versions — keep using it)
Dockerfile              image with ffmpeg + Chromium
docs/SCALING.md         ★ how to run many voices at once, limits, failure handling
docs/FRONTEND_GUIDE.md  ★ brief for a simple upload/progress/download UI (+ a prompt for Claude Code / Codex)
service/
  voice_notes/          ★ the entry point: process_audio(), CLI, selftest  (small, yours to extend)
  config/settings/service.py   service settings on top of base.py (apps, DB, local storage, v2.2 switches)
  config/settings/base.py      TesaNote's full settings: models, providers, limits (read-only reference)
  apps/ services/ infrastructure/   the production pipeline (do not rewrite)
  templates/ public/fonts scripts/  note HTML template, fonts, PDF renderer
```

## How a job works inside (for debugging)

`process_audio` creates a *Lecture* row (the pipeline's unit of work) + stores the audio under `VNS_DATA_DIR/media/lectures/<id>/`, marks it `note_style = "lecturer"` (this is what selects v2.2), then runs `LectureProcessingOrchestrator` exactly as production does:

| Stage | Code | Notes |
|---|---|---|
| audio clean-up, chunking | `apps/audio_pipeline/services.py` | ffmpeg; CPU heavy; chunks ≈ up to 10 min each |
| speech-to-text | `apps/audio_pipeline/transcription/service.py`, `services/transcription/providers/avalai_groq.py` | many small AvalAI calls per lecture; retries + fallback built in |
| note writing | `apps/notes/generation.py`, `quality_rev.py` (v2.2 rules) | several LLM calls; the biggest cost and time |
| rendering | `apps/notes/renderers.py`, `templates/` | pure CPU, fast |
| PDF (optional) | `apps/notes/services.py` → `scripts/render_note_pdf.mjs` | headless Chromium |

Every provider call is recorded in the database table `ai_costs_aiproviderrequest` (model, tokens, cost) — use it for cost reporting.

Working files are deleted after each job (`keep_working_files=False`); results are in `out_dir`.

## Things that are deliberately NOT here

* The student-facing app (UI/UX, auth, payments, wallet) – the service runs without billing (`WALLET_BILLING_ENABLED=False`).
* The "explained" v4.2 note, the study tools (chat, summary, flashcards, quiz), note editing.
* A job queue or HTTP API – you choose them (`docs/SCALING.md` recommends a shape).

If something you need seems missing, it was probably cut on purpose; ask the TesaNote team before adding the main app back.
