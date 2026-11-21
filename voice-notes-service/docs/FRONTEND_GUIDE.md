# A simple front end for the service

The service has **no UI and no API on purpose**. You need a small, plain wrapper that (1) takes voices in, (2) shows progress per voice, (3) gives the notes back. Nothing fancy: this is a working tool, not the TesaNote product. Keep it boring and reliable.

## What the screen must do

1. **Add voices** — drag-and-drop several files *or* pick a folder (`<input type="file" multiple webkitdirectory>`); show each file with name, size, length when known.
2. **Optional fields per batch:** course name, professor name (both optional); per file: title (default = file name).
3. **Start** — one button; uploads all files (with per-file upload progress), then creates one job per file.
4. **Progress list** — one row per voice with a status: `Uploading → Queued → Processing → Done / Failed`, elapsed time, and the failure reason in plain words when failed, with a **Retry** button.
5. **Results** — per row, when done: **Open note** (HTML in a new tab), **Download PDF** (if enabled), **Download transcript (.txt)**. Plus **Download all** (zip) for the batch.
6. Works on a phone (students will use it), right-to-left (`dir="rtl"`, `lang="fa"`), Persian digits optional.

Avoid: auth systems, dashboards, settings pages, animations. If login is required, use one shared password or the host platform's auth.

## The minimal backend contract (the wrapper you write around `process_audio`)

Any framework (FastAPI recommended). Four endpoints are enough:

| Method & path | Does | Returns |
|---|---|---|
| `POST /batches` (multipart: `files[]`, `course_name`, `professor_name`) | saves files, creates 1 job per file in *your* queue | `{batch_id, jobs:[{job_id, filename, status:"queued"}]}` |
| `GET /batches/{batch_id}` | current state of every job | `{jobs:[{job_id, filename, status, stage?, elapsed_seconds, error?, links:{note, pdf, transcript}}]}` |
| `GET /jobs/{job_id}/files/{note.html|note.pdf|transcript.txt|note.json}` | streams a result file | the file |
| `POST /jobs/{job_id}/retry` | re-queues a failed job | `{status:"queued"}` |

`status` ∈ `queued | processing | done | failed`. The page polls `GET /batches/{id}` every 3–5 seconds (no websockets needed).

How the worker maps to the service: `result = process_audio(path, out_dir=f"results/{job_id}")` → store `result.status`, `result.error`, `result.error_stage`, and the file paths. See `docs/SCALING.md` for running workers in parallel. The "stage" shown to students can be coarse (the service does not stream fine-grained progress; show elapsed time instead of a fake percentage).

Security basics: accept only audio types the service supports (README), cap size/count per batch (e.g. 20 files, 500 MB each), never expose `.env`, never put the AvalAI key in the browser, serve results only to the batch's owner (unguessable `batch_id` is the minimum).

## Prompt you can give Claude Code / Codex

Open the `voice-notes-service` folder as the project and paste this:

> Read `README.md`, `docs/SCALING.md` and `docs/FRONTEND_GUIDE.md` first. Build a small web app **around** the service in a new folder `app/` (do not modify anything under `service/apps`, `service/services` or `service/infrastructure`).
>
> **Backend:** FastAPI + a Postgres-backed job table (or Redis queue). Endpoints exactly as in the contract table of `docs/FRONTEND_GUIDE.md`. A worker command `python -m app.worker --concurrency N` that starts N separate processes; each pulls one queued job, calls `voice_notes.process_audio(...)` with a per-job `out_dir`, and records `status/error/error_stage/elapsed`. Fair scheduling: round-robin between students/batches and at most 4 running jobs per student. Requeue jobs whose worker died (heartbeat). Keep global concurrency configurable and default to 4.
>
> **Front end:** one static page (plain HTML + a little vanilla JS or a tiny Vite/React app — no heavy UI framework), RTL Persian (`lang="fa" dir="rtl"`), mobile-first. Multi-file drag-and-drop and folder picker; per-file upload progress; polling status list; per-row Open/Download buttons; "Download all" zip; clear Persian error messages with a Retry button. Calm, plain styling (system font stack, one accent colour, no gradients).
>
> **Done when:** (1) with the AvalAI key unset, the app starts and `python -m voice_notes selftest` shows what is missing; (2) a test that stubs `process_audio` shows 20 jobs flowing through the queue with the concurrency cap respected and one failure retried; (3) a README section explains how to run it. **Never run `process_audio` with a real key in automated tests — it spends money.**

(If your agent starts "improving" the pipeline code, stop it: the task is the wrapper, the queue and the page.)
