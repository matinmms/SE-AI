# Running many voices at once

This is the part of the project that is yours. This page tells you what the service guarantees, where the real limits are, and a recommended shape. **Measure before you tune** — start with 2 voices, then 5, then 10, 20; watch time, errors and AvalAI usage at each step.

## 1. What one job costs the machine and the provider

One `process_audio()` call = one voice:

| Resource | Usage |
|---|---|
| CPU | ffmpeg clean-up/chunking at the start (heavy but short), rendering at the end. Roughly 1 core busy during those parts, almost idle while waiting for AvalAI. |
| RAM | Moderate (a few hundred MB peak). |
| Disk | The audio + chunks while running (`VNS_DATA_DIR/media/lectures/<id>/`), deleted at the end. Plan for ~3× the audio size per running job. |
| AvalAI calls | Many small speech-to-text calls (about one per ≤10-minute chunk, with retries) **plus** several long note-writing LLM calls. The note-writing calls dominate time and cost. |
| Time | Dominated by provider latency, not your CPU. |

So the machine is rarely the bottleneck — **the AvalAI limits are.**

## 2. The real ceiling: provider limits

The preset limits (in `.env.example`, enforced inside the pipeline) are what TesaNote runs with:

```
AVALAI_RPM_LIMIT=45             requests per minute
AVALAI_TPM_LIMIT=450000         tokens per minute
AVALAI_PROVIDER_MAX_CONCURRENCY=4   simultaneous provider calls
```

With these, 20 voices started together will mostly **wait for each other** inside the provider gate — they will not all be "running" at full speed. Before raising anything:

1. Ask AvalAI what your account is actually allowed (RPM / TPM / concurrency), in writing if possible.
2. Raise the three values together, in `.env`, to ≤ what the account allows.
3. Re-measure. More workers than the provider allows only makes jobs wait (and eventually time out, see §5).

**The limits are shared across processes only when Redis is configured** (`REDIS_URL`). With Redis, 10 worker processes together respect 45 RPM. Without it each process limits only itself and 10 processes would send 10× the traffic → 429 errors. **Use Redis for any parallel run.**

## 3. Recommended shape

```
 students ─► your intake (HTTP API / upload) ─► queue (jobs table or Redis) ─► N worker PROCESSES
                                                                                  │ each: process_audio(...)
                                  progress/status ◄── your DB ◄───────────────────┘
                                  finished files  ◄── out_dir (disk or S3)
```

* **One worker process = one job at a time.** Start `N` worker processes (systemd units, containers, `multiprocessing`, Celery with `--concurrency N --pool prefork`, RQ, …). Do **not** run several jobs as threads inside one process: the pipeline keeps per-process state (DB connection, cost-tracking context) and was not designed for that.
* **Choose N from the provider limit, not from CPU count.** Start with N = `AVALAI_PROVIDER_MAX_CONCURRENCY` (4) and increase only after step 2 above.
* **Postgres, not SQLite**, once there is more than one process: set `VNS_DATABASE_URL=postgres://...` and run `python manage.py migrate` once.
* **Fairness between students:** the service knows nothing about students; your queue does. Simple rule that works: round-robin between students, so one student's 20 files cannot starve another's 2. Cap per-student running jobs (e.g. 4).
* **Idempotency:** give every voice your own `job id` in *your* table. If a worker dies, re-queue by your id; `process_audio` creates a fresh internal job each call (no duplicates are created by retries *inside* one call). Always write to a fresh `out_dir` per attempt, or delete the old one.
* **Failures are data:** `result.status == "FAILED"` with `error_stage`/`error`. Stages you will see: `VALIDATION` and `AUDIO_INPUT` (bad or unreadable audio), `NOTE_MODEL_PRECHECK` (the note models did not answer the pre-start health check — usually a bad key or a provider outage), `TRANSCRIPTION_TIMEOUT`, `PROCESSING_TIMEOUT`, `NOTE_GENERATION`, `SERVICE`/`SYSTEM` (unexpected). Retry transient ones (provider errors, timeouts) with back-off 2–3 times, then show the student a clear message; do not retry `VALIDATION`/`AUDIO_INPUT`.

## 4. "Sources" instead of separate notes

If 10–20 voices should become **one** note (all voices are sources for the same study set) rather than 20 notes:

* Transcribe each voice separately (in parallel), keep the order the student chose, join the transcripts, then generate **one** note from the joined text.
* The pipeline already supports generating a note from supplied text: `apps/notes/tasks.py → run_note_generation_sync(lecture_id=..., transcript_override="...")`, and the transcript step is `TranscriptionOrchestratorService` (`apps/audio_pipeline/transcription/service.py`). `Lecture.merged_transcript` is where the text lives.
* Mind the model's input limit (`AVALAI_TARGET_MAX_INPUT_TOKENS=120000`): very long combined transcripts need splitting into sections — the pipeline already chunks by sections; verify with a real long set.
* `process_audio()` today does the "one voice → one note" case. Add the "many → one" case as a **second function next to it** (in `service/voice_notes/`), reusing the same building blocks. Agree with the TesaNote team which behaviour the product wants *before* building it.

## 5. Timeouts you must know

* `LECTURE_PROCESSING_TIMEOUT_SECONDS=1800`: a job is cancelled automatically **30 minutes after `process_audio` starts it**. Your queue waiting time does **not** count (the clock starts inside the call), so queue freely. But a job that waits a long time *inside* the provider gate counts. If you see `PROCESSING_TIMEOUT` under load, either lower parallelism or raise this value (it is a deliberate safety net — raise it consciously).
* `AVALAI_PROVIDER_SLOT_WAIT_SECONDS=300`: how long a call waits for a provider slot before failing. Raise together with parallelism if you see slot-wait errors.
* `TRANSCRIPTION_CHUNK_INACTIVITY_TIMEOUT_SECONDS=600`: transcription cancelled if no chunk completes for 10 minutes.
* Max audio length 150 min (`MAX_AUDIO_DURATION_MINUTES`).

## 6. Cost and observability

* Every provider call is a row in `ai_costs_aiproviderrequest` (`lecture_id`, `requested_model`, tokens, `paid_irt`). Sum by `lecture_id` for the cost of one voice; log it next to your job id.
* Logs are JSON on stdout (`logger: tesanote.ai`, events such as `ai_provider_request_failed`). Ship them.
* Keep a dashboard of: queue length, running jobs, failures by `error_stage`, time per stage, AvalAI 429 count.

## 7. Rules that protect the service

* Do not change model/provider settings "to speed things up". They were chosen and tested together; changing them changes note quality and cost.
* Never delete `lectures/<id>` working files of a *running* job.
* Keep `constraints.txt` when installing — an uncached rebuild once pulled a newer `kombu` and broke all workers.
* Keep AvalAI's key out of logs, the front end and git.
