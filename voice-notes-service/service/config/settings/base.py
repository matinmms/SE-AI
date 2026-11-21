# NOTE: copy of TesaNote's backend/config/settings/base.py for the standalone voice->note service.
# Only change from the original: the staff/test phone-number allow-lists are emptied (not needed here).
from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import environ
from corsheaders.defaults import default_headers
from django_celery_beat.tzcrontab import TzAwareCrontab
from django.core.exceptions import ImproperlyConfigured

from infrastructure.logging.formatters import JsonFormatter

BASE_DIR = Path(__file__).resolve().parents[2]
APPS_DIR = BASE_DIR / "apps"
_DEFAULT_BUNDLED_NODE = (
    Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
)
_DEFAULT_PLAYWRIGHT_MODULE = (
    Path.home()
    / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright"
)
_DEFAULT_CHROME_EXECUTABLE = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
_DEFAULT_NOTE_PDF_NODE = (
    str(_DEFAULT_BUNDLED_NODE)
    if _DEFAULT_BUNDLED_NODE.exists()
    else (shutil.which("node") or "node")
)
_DEFAULT_NOTE_PDF_PLAYWRIGHT = (
    str(_DEFAULT_PLAYWRIGHT_MODULE) if _DEFAULT_PLAYWRIGHT_MODULE.exists() else "playwright"
)
_DEFAULT_NOTE_PDF_BROWSER = (
    str(_DEFAULT_CHROME_EXECUTABLE) if _DEFAULT_CHROME_EXECUTABLE.exists() else ""
)
IRAN_BUSINESS_TIME_ZONE = ZoneInfo("Asia/Tehran")

env = environ.Env(
    DEBUG=(bool, False),
    DJANGO_SECRET_KEY=(str, "unsafe-dev-secret-key"),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1", "0.0.0.0"]),
    DJANGO_CORS_ALLOWED_ORIGINS=(list, ["http://localhost:3000", "http://127.0.0.1:3000"]),
    DJANGO_CSRF_TRUSTED_ORIGINS=(list, ["http://localhost:3000", "http://127.0.0.1:3000"]),
    POSTGRES_DB=(str, "tesanote"),
    POSTGRES_USER=(str, "tesanote"),
    POSTGRES_PASSWORD=(str, "tesanote"),
    POSTGRES_HOST=(str, "postgres"),
    POSTGRES_PORT=(int, 5432),
    REDIS_URL=(str, "redis://redis:6379/0"),
    CELERY_BROKER_URL=(str, "redis://redis:6379/1"),
    CELERY_RESULT_BACKEND=(str, "redis://redis:6379/2"),
    CELERY_TASK_DEFAULT_QUEUE=(str, "default"),
    CELERY_TASK_ALWAYS_EAGER=(bool, False),
    CELERY_WORKER_PREFETCH_MULTIPLIER=(int, 1),
    CELERY_WORKER_MAX_TASKS_PER_CHILD=(int, 50),
    CELERY_BROKER_POOL_LIMIT=(int, 10),
    CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS=(int, 3600),
    CELERY_RESULT_EXPIRES_SECONDS=(int, 3600),
    DYNAMIC_CONCURRENCY_ENABLED=(bool, False),
    PROCESSING_CONCURRENCY_MIN=(int, 1),
    PROCESSING_CONCURRENCY_MAX=(int, 2),
    PROCESSING_CONCURRENCY_HARD_MAX=(int, 2),
    PROCESSING_CONCURRENCY_ABSOLUTE_MAX=(int, 8),
    RESOURCE_SAMPLE_INTERVAL_SECONDS=(int, 30),
    RESOURCE_SAMPLE_MAX_AGE_SECONDS=(int, 90),
    CONCURRENCY_SCALE_UP_STABLE_SECONDS=(int, 120),
    CPU_SCALE_UP_THRESHOLD=(float, 0.60),
    MEMORY_SCALE_UP_MIN_AVAILABLE_BYTES=(int, 1_000_000_000),
    SWAP_SCALE_UP_MAX_USED_BYTES=(int, 256_000_000),
    DISK_SAFETY_MIN_FREE_BYTES=(int, 2_000_000_000),
    PROCESSING_WORKER_HOST_ID=(str, "web-local"),
    PROCESSING_WORKER_HOST_IDS=(list, ["web-local"]),
    DISTRIBUTED_CAPACITY_ENABLED=(bool, False),
    PROCESSING_GLOBAL_MAX_SLOTS=(int, 2),
    PROCESSING_HOST_MAX_SLOTS=(int, 2),
    NOTE_PROVIDER_MAX_CONCURRENCY=(int, 4),
    NOTE_STRUCTURE_PARALLELISM=(int, 2),
    NOTE_TOPIC_PLAN_DIGEST_ENABLED=(bool, False),
    NOTE_PDF_DEFERRED_ENABLED=(bool, False),
    SLIDE_OCR_ENABLED=(bool, True),
    SLIDE_OCR_MIN_TEXT_CHARS=(int, 30),
    SLIDE_OCR_MIN_IMAGE_PX=(int, 120),
    SLIDE_OCR_MIN_CONFIDENCE=(float, 0.5),
    SLIDE_OCR_MAX_IMAGES=(int, 300),
    SLIDE_OCR_THREADS=(int, 2),
    SLIDE_OCR_TIMEOUT_SECONDS=(int, 300),
    NOTE_PROVIDER_SLOT_WAIT_SECONDS=(int, 300),
    NOTE_PROVIDER_SLOT_POLL_SECONDS=(float, 0.5),
    PDF_RENDER_MAX_CONCURRENCY=(int, 1),
    PDF_RENDER_SLOT_WAIT_SECONDS=(int, 300),
    PDF_RENDER_SLOT_POLL_SECONDS=(float, 0.5),
    AVANEGAR_PROVIDER_MAX_CONCURRENCY=(int, 1),
    AVANEGAR_PROVIDER_SLOT_WAIT_SECONDS=(int, 300),
    AVANEGAR_PROVIDER_SLOT_POLL_SECONDS=(float, 0.25),
    LECTURE_TASK_LOCK_TTL_SECONDS=(int, 3600),
    LECTURE_PROCESSING_TIMEOUT_SECONDS=(int, 1800),
    TRANSCRIPTION_CHUNK_INACTIVITY_TIMEOUT_SECONDS=(int, 600),
    NOTE_GENERATION_INACTIVITY_TIMEOUT_SECONDS=(int, 600),
    LOG_LEVEL=(str, "INFO"),
    SCALING_BASELINE_METRICS_ENABLED=(bool, False),
    SCALING_BASELINE_METRICS_SAMPLE_RATE=(float, 1.0),
    AI_COST_TRACKING_ENABLED=(bool, False),
    AI_COST_RECONCILIATION_ENABLED=(bool, False),
    AI_COST_RECONCILIATION_INITIAL_DELAY_SECONDS=(int, 60),
    AI_COST_RECONCILIATION_BATCH_SIZE=(int, 100),
    AI_COST_RECONCILIATION_FORCE_AFTER_HOURS=(int, 24),
    AI_COST_RECONCILIATION_HARD_MAX_AGE_DAYS=(int, 7),
    SECURE_SSL_REDIRECT=(bool, False),
    SESSION_COOKIE_SECURE=(bool, False),
    CSRF_COOKIE_SECURE=(bool, False),
    HEALTH_CHECK_REDIS_ENABLED=(bool, True),
    # Whole lectures recorded as video or lossless audio reach 300-850 MB; they upload in
    # 2 MB chunks, so the limit is about disk and patience, not request size.
    AUDIO_MAX_UPLOAD_SIZE_MB=(int, 1024),
    SLIDES_MAX_UPLOAD_SIZE_MB=(int, 100),
    MAX_AUDIO_DURATION_MINUTES=(int, 150),
    AUDIO_MAX_DURATION_SECONDS=(int, 0),
    ANON_UPLOAD_ENABLED=(bool, True),
    ANON_UPLOAD_TTL_HOURS=(int, 24),
    ANON_UPLOAD_MAX_PER_IP_PER_DAY=(int, 5),
    FIRST_NOTE_FULL_ENABLED=(bool, True),
    FIRST_NOTE_FULL_MIN_BALANCE_MINUTES=(int, 10),
    ANON_UPLOAD_MAX_LIVE_UPLOADS=(int, 300),
    # Off by default: every paid plan already sells at its own 25% public discount
    # (list price struck through), so a second 25% would undercut the stated price.
    FIRST_PURCHASE_DISCOUNT_ENABLED=(bool, False),
    FIRST_PURCHASE_DISCOUNT_PERCENT=(int, 25),
    FIRST_PURCHASE_DISCOUNT_HOURS=(int, 72),
    ANALYTICS_EXCLUDED_ACCOUNTS=(list, []),
    PERF_METRICS_ENABLED=(bool, True),
    PERF_FLUSH_SECONDS=(int, 60),
    PERF_RETENTION_DAYS=(int, 30),
    FFMPEG_BINARY=(str, "ffmpeg"),
    FFPROBE_BINARY=(str, "ffprobe"),
    GAPGPT_API_KEY=(str, ""),
    PRIMARY_AI_API_KEY=(str, ""),
    PRIMARY_AI_BASE_URL=(str, "https://api.gapgpt.app/v1"),
    PRIMARY_AI_MAX_RETRIES=(int, 2),
    PRIMARY_AI_RETRY_BASE_DELAY_SECONDS=(float, 2.0),
    GAPGPT_WHISPER_PRICE_PER_MINUTE=(float, 0.006),
    GAPGPT_MAX_CLIP_DURATION_SECONDS=(float, 29.0),
    GAPGPT_WHISPER_MIN_JOB_COST=(float, 0.004),
    GAPGPT_TRANSCRIPTION_MIN_CHUNK_SECONDS=(float, 4.0),
    GAPGPT_TRANSCRIPTION_TIMEOUT_SECONDS=(int, 60),
    AVANEGAR_API_TOKEN=(str, ""),
    AVANEGAR_BASE_URL=(str, "https://partai.gw.isahab.ir"),
    AVANEGAR_REQUEST_PATH=(str, "/avanegar/v2/avanegar/request"),
    AVANEGAR_REQUEST_TIMEOUT_SECONDS=(int, 45),
    AVANEGAR_REQUEST_MAX_RETRIES=(int, 3),
    AVANEGAR_REQUEST_RETRY_BASE_DELAY_SECONDS=(float, 2.0),
    AVANEGAR_BILLING_UNIT_SECONDS=(float, 15.0),
    AVANEGAR_TARGET_CHUNK_DURATION_SECONDS=(float, 59.0),
    AVANEGAR_MAX_CHUNK_DURATION_SECONDS=(float, 59.5),
    DADMATECH_API_TOKEN=(str, ""),
    DADMATECH_BASE_URL=(str, "https://dadmatech.gw.isahab.ir"),
    STT_CONTEXT_PROMPT_ENABLED=(bool, True),
    HOTWORDS_MAX_COUNT=(int, 50),
    HOTWORDS_MAX_LENGTH=(int, 40),
    AUDIO_FAST_NORMALIZATION=(bool, True),
    AUDIO_ENABLE_HEAVY_DENOISE=(bool, False),
    AUDIO_ENABLE_LOUDNORM=(bool, False),
    AUDIO_SEGMENTED_PIPELINE_ENABLED=(bool, True),
    AUDIO_SEGMENTED_PIPELINE_MIN_DURATION_SECONDS=(int, 900),
    AUDIO_SEGMENT_SECONDS=(int, 60),
    AUDIO_SEGMENT_OVERLAP_SECONDS=(int, 0),
    AUDIO_FFMPEG_THREADS=(int, 1),
    AUDIO_PIPELINE_MAX_CONCURRENCY=(int, 1),
    AUDIO_DELETE_TEMP_SEGMENTS_AFTER_SUCCESS=(bool, True),
    AUDIO_KEEP_CHUNKS_AFTER_TRANSCRIPTION=(bool, True),
    AUDIO_VAD_PADDING_SECONDS=(float, 0.45),
    AUDIO_MIN_SPEECH_RATIO_FLOOR=(float, 0.55),
    AUDIO_MIN_REMOVABLE_SILENCE_SECONDS=(float, 1.4),
    TRANSCRIPTION_BATCH_SIZE=(int, 5),
    TRANSCRIPTION_SUBMIT_PARALLELISM=(int, 2),
    TRANSCRIPTION_RECOVERY_ENABLED=(bool, True),
    TRANSCRIPTION_STUCK_TIMEOUT_SECONDS=(int, 300),
    TRANSCRIPTION_SUBMISSION_STALE_SECONDS=(int, 60),
    NOTE_LLM_BASE_URL=(str, "https://api.gapgpt.app/v1"),
    # Note generation has its own provider route. It is intentionally separate
    # from the study tutor and transcription providers.
    NOTE_PRIMARY_PROVIDER=(str, "avalai"),
    NOTE_PRIMARY_MODEL=(str, "gpt-5.6-luna"),
    NOTE_FALLBACK_PROVIDER=(str, "gapgpt"),
    NOTE_FALLBACK_MODEL=(str, "gemini-2.5-flash-lite"),
    NOTE_AB_TEST_MODELS=(list, ["gpt-5.6-luna"]),
    NOTE_LLM_TEMPERATURE=(float, 0.2),
    NOTE_LLM_MAX_RETRIES=(int, 4),
    NOTE_LLM_RETRY_BASE_DELAY_SECONDS=(float, 4.0),
    NOTE_LLM_TIMEOUT_SECONDS=(int, 90),
    NOTE_LLM_MAX_OUTPUT_TOKENS=(int, 3500),
    # Optional model-level readiness candidates. Provider fallback is configured
    # above and runs during generation when the primary provider fails.
    NOTE_FALLBACK_MODELS=(list, []),
    # Structure-first generation pipeline (A/B test): a whole-transcript
    # structuring pass followed by topic-grouped writing, gated to specific
    # users by id while the A/B test is running. Empty list / disabled means
    # every lecture uses the existing (v1) pipeline, unchanged.
    NOTE_GENERATION_V2_ENABLED=(bool, True),
    NOTE_GENERATION_V2_MODEL=(str, "gpt-6-luna"),
    # Professor-rewrite pipeline (v3, A/B test): the model re-teaches the
    # lecture in its own order instead of following the lecturer's. Gated to
    # an explicit allowlist of user ids; empty list means nobody uses it.
    NOTE_GENERATION_V3_ENABLED=(bool, True),
    NOTE_GENERATION_V3_MODEL=(str, "gpt-6-luna"),
    NOTE_GENERATION_V3_USER_IDS=(list, []),
    # True: every user gets v3. False: only NOTE_GENERATION_V3_USER_IDS do.
    NOTE_GENERATION_V3_FOR_ALL=(bool, True),
    # v4 (pedagogy-first teaching style, built on the v3 pipeline). Allowlist is
    # by login phone number (09XXXXXXXXX); an empty list turns v4 off for all.
    NOTE_GENERATION_V4_ENABLED=(bool, True),
    # True: every user gets v4. False: only NOTE_GENERATION_V4_PHONE_NUMBERS do.
    NOTE_GENERATION_V4_FOR_ALL=(bool, True),
    NOTE_GENERATION_V4_PHONE_NUMBERS=(list, []),
    # Admin note comparison: for these staff accounts every finished lecture also gets a second
    # note written by the v2 (lecturer's-own-words) pipeline, next to the normal v4 note, so the
    # two can be compared. Login phone numbers (09XXXXXXXXX); an empty list or the flag off
    # turns it off. Costs one extra note generation per lecture for those accounts only.
    NOTE_COMPARISON_V2_ENABLED=(bool, True),
    NOTE_COMPARISON_V2_PHONE_NUMBERS=(list, []),
    # Note quality revision 2 ("v4.1" / "v2.1"): meta-commentary scrubbing, merged exam boxes, a
    # summary that covers every section, number/term fidelity and teaching-story rules. Admin
    # trial only: staff accounts in this list. Everyone else keeps the plain v4 / v2 behaviour.
    NOTE_QUALITY_V21_ENABLED=(bool, True),
    NOTE_QUALITY_V21_PHONE_NUMBERS=(list, []),
    NOTE_QUALITY_V42_ENABLED=(bool, True),
    NOTE_QUALITY_V42_PHONE_NUMBERS=(list, []),
    NOTE_QUALITY_V42_FOR_ALL=(bool, True),
    # 2026-11-18 fix plan, phase B. GUARDS: kill switch for the transcript-based guards (Latin-term
    # report, unit-change report, exam box needs a cue). TERM_GUARD: off | report | enforce.
    # CHUNK_MARKS: tell the writer which stretches of its excerpts the recognizer broke.
    NOTE_QUALITY_V42_GUARDS_ENABLED=(bool, True),
    NOTE_QUALITY_V42_TERM_GUARD=(str, "report"),
    NOTE_QUALITY_V42_CHUNK_MARKS_ENABLED=(bool, True),
    # v4.1: the quality revision for the main (v4 teaching) note of EVERY user. The v2.1 side of the
    # revision (the lecturer's-words comparison note) stays with the staff list above. False returns
    # to the admin-only trial.
    NOTE_QUALITY_V41_FOR_ALL=(bool, True),
    # Where the upload page learns the visitor's country (VPN hint). Headers a
    # CDN/proxy may set, in priority order, and an optional GeoLite2/GeoIP2
    # country database file; with neither, the server reports "unknown".
    CLIENT_COUNTRY_HEADERS=(list, ["CF-IPCountry", "X-Client-Country", "X-Country-Code"]),
    CLIENT_GEOIP_COUNTRY_DB=(str, ""),
    # v3 length discipline. A study note is a selection, not a transcript: the
    # target size is derived from the source length and then capped, so a long
    # lecture cannot grow an unbounded note. Ratios are note chars per
    # transcript char; the caps correspond to roughly 15 and 7 rendered pages.
    NOTE_V3_DETAILED_SOURCE_RATIO=(float, 0.50),
    NOTE_V3_SUMMARIZED_SOURCE_RATIO=(float, 0.18),
    NOTE_V3_MAX_NOTE_CHARS=(int, 21000),
    NOTE_V3_MAX_SUMMARIZED_NOTE_CHARS=(int, 7000),
    NOTE_V3_MIN_NOTE_CHARS=(int, 4000),
    # How far a topic may exceed its allocation before the condense pass runs.
    NOTE_V3_BUDGET_TOLERANCE=(float, 0.15),
    # Kill switch for the condense/trim pass only; the budget still reaches the
    # prompts when this is off, so turning it off degrades rather than reverts.
    NOTE_V3_ENFORCE_BUDGET=(bool, True),
    # The budget is a target range, not only a ceiling: a topic that comes back
    # below this fraction of what its source can justify is written again once.
    NOTE_V3_MIN_FILL_RATIO=(float, 0.6),
    NOTE_V3_EXPAND_THIN_TOPICS=(bool, True),
    # The study tutor is isolated from note-generation provider settings.
    # AvalAI exposes Groq's model through its existing unified API and key.
    CHAT_MODEL_PROVIDER=(str, "avalai"),
    CHAT_MODEL_NAME=(str, "groq.gpt-oss-20b"),
    CHAT_MODEL_MAX_OUTPUT_TOKENS=(int, 2048),
    CHAT_MODEL_TEMPERATURE=(float, 0.25),
    # Temporarily disable user-facing tutor quotas; usage/cost logging stays active.
    CHAT_QUOTA_ENABLED=(bool, False),
    CHAT_MONTHLY_COST_CAP_USD=(str, "0.05"),
    CHAT_5_HOUR_COST_CAP_USD=(str, "0.005"),
    CHAT_WEEKLY_COST_CAP_USD=(str, "0.0125"),
    GROQ_API_KEY=(str, ""),
    CHAT_GROQ_API_KEY=(str, ""),
    # Leave blank to use the selected provider's canonical OpenAI-compatible URL.
    CHAT_MODEL_BASE_URL=(str, ""),
    CHAT_MODEL_TIMEOUT_SECONDS=(float, 20.0),
    CHAT_MODEL_MAX_RETRIES=(int, 1),
    EMERGENCY_AI_PRIMARY_PROVIDER=(str, "gapgpt"),
    EMERGENCY_AI_PRIMARY_MODEL=(str, "gapgpt-deepseek-v4.1-flash"),
    EMERGENCY_AI_FALLBACK_PROVIDER=(str, "arvan"),
    EMERGENCY_AI_FALLBACK_MODEL=(str, "Qwen3.5-35B-A3B"),
    EMERGENCY_CHAT_PROVIDER=(str, "arvan"),
    EMERGENCY_CHAT_MODEL=(str, "Xerxes-1"),
    EMERGENCY_CHAT_FALLBACK_PROVIDER=(str, "gapgpt"),
    EMERGENCY_CHAT_FALLBACK_MODEL=(str, "gapgpt-deepseek-v4.1-flash"),
    TEXT_NOTE_MIN_MINUTES=(int, 5),
    TEXT_NOTE_MAX_CHARS=(int, 60000),
    ARVAN_AI_API_KEY=(str, ""),
    ARVAN_AI_BASE_URL=(str, "https://api.arvancloudai.ir/v1"),
    ARVAN_AI_MODEL=(str, "Qwen3.5-35B-A3B"),
    ARVAN_AI_TIMEOUT_SECONDS=(int, 180),
    ARVAN_AI_MAX_RETRIES=(int, 1),
    ARVAN_AI_RETRY_BASE_DELAY_SECONDS=(float, 1.5),
    ARVAN_AI_PROVIDER_MAX_CONCURRENCY=(int, 4),
    ARVAN_AI_PROVIDER_SLOT_WAIT_SECONDS=(int, 300),
    ARVAN_AI_PROVIDER_SLOT_POLL_SECONDS=(float, 0.5),
    ARVAN_AI_RPM_LIMIT=(int, 45),
    ARVAN_AI_TPM_LIMIT=(int, 450000),
    ARVAN_AI_RATE_LIMIT_WAIT_SECONDS=(int, 300),
    ARVAN_AI_RATE_LIMIT_POLL_SECONDS=(float, 1.0),
    ARVAN_AI_TARGET_MAX_OUTPUT_TOKENS=(int, 8192),
    ARVAN_AI_DISABLE_THINKING=(bool, True),
    AVALAI_API_KEY=(str, ""),
    AVALAI_BASE_URL=(str, "https://api.avalai.ir/v1"),
    AVALAI_MODEL=(str, "gpt-5.6-luna"),
    AVALAI_STT_MODEL=(str, "groq.whisper-large-v3-turbo"),
    AVALAI_STT_TIMEOUT_SECONDS=(int, 90),
    AVALAI_STT_MAX_RETRIES=(int, 1),
    AVALAI_STT_LANGUAGE=(str, "fa"),
    AVALAI_STT_RESPONSE_FORMAT=(str, "text"),
    AVALAI_STT_TEMPERATURE=(float, 0.0),
    AVALAI_MAX_RETRIES=(int, 1),
    AVALAI_RETRY_BASE_DELAY_SECONDS=(float, 1.5),
    AVALAI_TIMEOUT_SECONDS=(int, 90),
    AVALAI_PROVIDER_MAX_CONCURRENCY=(int, 4),
    AVALAI_PROVIDER_SLOT_WAIT_SECONDS=(int, 300),
    AVALAI_PROVIDER_SLOT_POLL_SECONDS=(float, 0.5),
    AVALAI_RPM_LIMIT=(int, 45),
    AVALAI_TPM_LIMIT=(int, 450000),
    AVALAI_RATE_LIMIT_WAIT_SECONDS=(int, 300),
    AVALAI_RATE_LIMIT_POLL_SECONDS=(float, 1.0),
    AVALAI_TARGET_MAX_INPUT_TOKENS=(int, 120000),
    AVALAI_TARGET_MAX_OUTPUT_TOKENS=(int, 6000),
    AVALAI_SLIDE_CONTEXT_CHAR_LIMIT=(int, 2200),
    AVALAI_TRANSCRIPT_OUTLINE_CHAR_LIMIT=(int, 1200),
    TRANSCRIPTION_PRIMARY_PROVIDER=(str, "avalai_groq_whisper"),
    TRANSCRIPTION_FALLBACK_PROVIDER=(str, "current"),
    MAX_AVALAI_STT_FILE_MB=(int, 25),
    TRANSCRIPTION_CHUNK_TARGET_MB=(int, 20),
    TRANSCRIPTION_CHUNK_MAX_SECONDS=(int, 600),
    WALLET_BILLING_ENABLED=(bool, True),
    WALLET_BILLING_FOR_ALL=(bool, False),
    WALLET_SHOW_PLAN_LABEL=(bool, False),
    WALLET_BILLING_ROLLOUT_PERCENT=(int, 0),
    WALLET_BILLING_PHONE_NUMBERS=(list, []),
    FRESH_ACCOUNT_PHONE_NUMBERS=(list, []),
    WALLET_PROFIT_TOMAN_PER_MINUTE=(int, 216),
    WALLET_STT_TOMAN_PER_MINUTE=(int, 92),
    WALLET_HOLD_FACTOR_PERCENT=(int, 130),
    WALLET_MIN_NOTE_CHARGE_TOMAN=(int, 2000),
    WALLET_ADDON_MARKUP_PERCENT=(int, 0),
    WALLET_ADDON_MIN_BALANCE_TOMAN=(int, 500),
    # Add-ons on the user's first note are free; chat is limited to this many messages (then they top up).
    WALLET_FIRST_NOTE_FREE_CHAT_MESSAGES=(int, 5),
    WALLET_MIN_TOPUP_TOMAN=(int, 50000),
    WALLET_MAX_TOPUP_TOMAN=(int, 5000000),
    WALLET_OFFER_MINUTES=(int, 15),
    WALLET_GIFT_TIERS=(str, "150000:20,300000:30,600000:40"),
    WALLET_GIFT_EXPIRY_DAYS=(int, 60),
    WALLET_LEGACY_GIFT_EXPIRY_DAYS=(int, 30),
    WALLET_LEGACY_CUTOFF=(str, "2026-11-13T00:00:00+00:00"),
    # Note style choice (explained / lecturer / transcript). Staff only until v2.2 passes review.
    NOTE_STYLE_CHOICE_ENABLED=(bool, True),
    NOTE_STYLE_CHOICE_STAFF_ONLY=(bool, True),
    NOTE_STYLE_TRANSCRIPT_PRICE_PERCENT=(int, 50),
    NOTE_REVIEW_CAPTURE_ENABLED=(bool, False),
    NOTE_REVIEW_FOR_ALL=(bool, True),
    # The count follows the lecture: at most 10% of its decode windows, never a fixed number.
    NOTE_REVIEW_MAX_SPOTS=(int, 25),
    NOTE_REVIEW_MAX_FLAG_RATIO=(float, 0.10),
    NOTE_REVIEW_MIN_BADNESS=(float, 0.5),
    NOTE_REVIEW_MIN_ALIGNMENT_SCORE=(float, 0.45),
    NOTE_REVIEW_CLIP_SECONDS=(int, 30),
    NOTE_REVIEW_CLIP_LEAD_SECONDS=(int, 2),
    NOTE_REVIEW_CLIP_BITRATE_KBPS=(int, 32),
    NOTE_REVIEW_DURATION_TOLERANCE_SECONDS=(float, 0.25),
    NOTE_REVIEW_CLIP_URL_TTL_SECONDS=(int, 300),
    NOTE_REVIEW_SHOW_PERCENT=(bool, False),
    NOTE_SUMMARY_CHUNK_CHARS=(int, 5000),
    NOTE_LONG_TRANSCRIPT_THRESHOLD_CHARS=(int, 12000),
    NOTE_SEMANTIC_BATCH_CHARS=(int, 8000),
    NOTE_PDF_NODE_EXECUTABLE=(str, _DEFAULT_NOTE_PDF_NODE),
    NOTE_PDF_PLAYWRIGHT_MODULE=(str, _DEFAULT_NOTE_PDF_PLAYWRIGHT),
    NOTE_PDF_BROWSER_EXECUTABLE=(str, _DEFAULT_NOTE_PDF_BROWSER),
    NOTE_PDF_RENDER_TIMEOUT_SECONDS=(int, 90),
    BACKEND_PUBLIC_BASE_URL=(str, "http://localhost:8000"),
    PAYMENT_PROVIDER=(str, "zibal"),
    PAYMENT_MOCK_ENABLED=(bool, True),
    PAYMENT_FRONTEND_BASE_URL=(str, "http://localhost:3000"),
    PAYMENT_CALLBACK_PATH=(str, "/api/v1/payments/callback/"),
    ZARINPAL_MERCHANT_ID=(str, ""),
    ZARINPAL_REQUEST_URL=(str, "https://api.zarinpal.com/pg/v4/payment/request.json"),
    ZARINPAL_VERIFY_URL=(str, "https://api.zarinpal.com/pg/v4/payment/verify.json"),
    ZARINPAL_START_PAY_BASE_URL=(str, "https://www.zarinpal.com/pg/StartPay/"),
    ZIBAL_MERCHANT=(str, "zibal"),
    ZIBAL_REQUEST_URL=(str, "https://gateway.zibal.ir/v1/request"),
    ZIBAL_VERIFY_URL=(str, "https://gateway.zibal.ir/v1/verify"),
    ZIBAL_INQUIRY_URL=(str, "https://gateway.zibal.ir/v1/inquiry"),
    ZIBAL_START_URL=(str, "https://gateway.zibal.ir/start"),
    ZIBAL_CONNECT_TIMEOUT_SECONDS=(float, 3.0),
    ZIBAL_READ_TIMEOUT_SECONDS=(float, 12.0),
    ZIBAL_VERIFY_MAX_RETRIES=(int, 2),
    PAYMENT_SESSION_TTL_MINUTES=(int, 20),
    PAYMENT_RECONCILE_STALE_SECONDS=(int, 45),
    ADMIN_OPS_RESOLVED_ERROR_RETENTION_DAYS=(int, 14),
    ADMIN_OPS_UNRESOLVED_ERROR_RETENTION_DAYS=(int, 14),
    ADMIN_OPS_RETENTION_BATCH_SIZE=(int, 500),
    ACCESS_TOKEN_LIFETIME_MINUTES=(int, 15),
    ACCESS_TOKEN_LIFETIME_DAYS=(int, 0),
    REFRESH_TOKEN_LIFETIME_DAYS=(int, 30),
    JWT_REFRESH_COOKIE_NAME=(str, "tesanote_refresh"),
    JWT_REFRESH_COOKIE_PATH=(str, "/api/auth/"),
    JWT_REFRESH_COOKIE_SECURE=(bool, False),
    JWT_REFRESH_COOKIE_SAMESITE=(str, "Lax"),
    JWT_REFRESH_COOKIE_DOMAIN=(str, ""),
    MELIPAYAMAK_USERNAME=(str, ""),
    MELIPAYAMAK_API_KEY=(str, ""),
    MELIPAYAMAK_BODY_ID=(int, 468754),
    MELIPAYAMAK_BASE_URL=(str, "https://rest.payamak-panel.com/api/SendSMS/BaseServiceNumber"),
    # Approved pattern (الگو) id for the "your note is ready" SMS; 0 = not set, nothing is sent.
    MELIPAYAMAK_BODY_ID_NOTE_READY=(int, 0),
    MELIPAYAMAK_SENDER_NUMBER=(str, ""),
    MELIPAYAMAK_TEXT_URL=(str, "https://rest.payamak-panel.com/api/SendSMS/SendSMS"),
    SMS_WINBACK_ENABLED=(bool, False),
    SMS_WINBACK_MIN_AGE_HOURS=(int, 24),
    SMS_WINBACK_TEST_NUMBERS=(list, []),
    # SMS automation. Everything is off until the founder turns it on, and DRY_RUN (rows and
    # logs, no provider call) stays on until a real delivery was checked on a phone.
    SMS_AUTOMATION_ENABLED=(bool, False),
    SMS_AUTOMATION_DRY_RUN=(bool, True),
    SMS_NOTE_READY_ENABLED=(bool, False),
    # Minutes after the note is ready before an unopened note gets the SMS.
    SMS_NOTE_READY_DELAY_MINUTES=(int, 30),
    # Notes older than this are never texted (so switching it on never reaches an old backlog).
    SMS_NOTE_READY_MAX_AGE_HOURS=(int, 24),
    # Share of users (0-100) who deliberately get no SMS, for a live control group. 0 = everyone
    # gets it (the founder compares against the period before the feature instead).
    SMS_HOLDOUT_PERCENT=(int, 0),
    SMS_MAX_PER_HOUR=(int, 30),
    SMS_MONTHLY_BUDGET_TOMAN=(int, 100000),
    SMS_COST_PER_PART_TOMAN=(int, 150),
    SMS_NOTE_READY_PARTS=(int, 2),
    # Comma-separated phone numbers; when set, ONLY these receive anything (staged real test).
    SMS_TEST_ALLOWLIST=(list, []),
    OTP_LENGTH=(int, 5),
    OTP_EXPIRY_SECONDS=(int, 180),
    OTP_RESEND_COOLDOWN_SECONDS=(int, 60),
    OTP_MAX_REQUESTS_PER_HOUR=(int, 5),
    OTP_MAX_VERIFY_ATTEMPTS=(int, 5),
    OTP_LOCK_SECONDS=(int, 600),
    LOCAL_AUTH_BYPASS=(bool, False),
    STORAGE_MODE=(str, "local"),
    OBJECT_STORAGE_ENDPOINT=(str, ""),
    OBJECT_STORAGE_REGION=(str, ""),
    OBJECT_STORAGE_BUCKET=(str, ""),
    OBJECT_STORAGE_KEY_PREFIX=(str, ""),
    OBJECT_STORAGE_ACCESS_KEY=(str, ""),
    OBJECT_STORAGE_SECRET_KEY=(str, ""),
    OBJECT_STORAGE_ADDRESSING_STYLE=(str, "path"),
    OBJECT_STORAGE_SIGNATURE_VERSION=(str, "s3v4"),
    OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS=(int, 5),
    OBJECT_STORAGE_READ_TIMEOUT_SECONDS=(int, 30),
    OBJECT_STORAGE_VERIFY_TLS=(bool, True),
    TEMP_STORAGE_TTL_HOURS=(int, 24),
    TEMP_STORAGE_CLEANUP_ENABLED=(bool, False),
    TEMP_STORAGE_CLEANUP_BATCH_SIZE=(int, 100),
    TEMP_STORAGE_CLEANUP_HOUR=(int, 4),
    TEMP_STORAGE_CLEANUP_CLAIM_TIMEOUT_MINUTES=(int, 60),
    PERSISTENT_STORAGE_QUOTA_BYTES=(int, 500_000_000),
    PERSISTENT_STORAGE_QUOTA_ENFORCEMENT_ENABLED=(bool, False),
    DIRECT_UPLOAD_ENABLED=(bool, False),
    DIRECT_UPLOAD_STAFF_ONLY=(bool, True),
    DIRECT_UPLOAD_URL_TTL_MINUTES=(int, 15),
    DIRECT_UPLOAD_SESSION_TTL_HOURS=(int, 2),
    DIRECT_UPLOAD_CLEANUP_HOUR=(int, 5),
    DIRECT_UPLOAD_CLEANUP_MINUTE=(int, 0),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
LOCAL_AUTH_BYPASS = env.bool("LOCAL_AUTH_BYPASS", default=False)
STORAGE_MODE = env("STORAGE_MODE").strip().lower()
if STORAGE_MODE not in {"local", "hybrid", "object"}:
    raise ImproperlyConfigured("STORAGE_MODE must be one of: local, hybrid, object.")

OBJECT_STORAGE_ENDPOINT = env("OBJECT_STORAGE_ENDPOINT").strip()
OBJECT_STORAGE_REGION = env("OBJECT_STORAGE_REGION").strip()
OBJECT_STORAGE_BUCKET = env("OBJECT_STORAGE_BUCKET").strip()
OBJECT_STORAGE_KEY_PREFIX = env("OBJECT_STORAGE_KEY_PREFIX").strip("/")
OBJECT_STORAGE_ACCESS_KEY = env("OBJECT_STORAGE_ACCESS_KEY").strip()
OBJECT_STORAGE_SECRET_KEY = env("OBJECT_STORAGE_SECRET_KEY").strip()
OBJECT_STORAGE_ADDRESSING_STYLE = env("OBJECT_STORAGE_ADDRESSING_STYLE").strip().lower()
OBJECT_STORAGE_SIGNATURE_VERSION = env("OBJECT_STORAGE_SIGNATURE_VERSION").strip()
OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS = env.int("OBJECT_STORAGE_CONNECT_TIMEOUT_SECONDS")
OBJECT_STORAGE_READ_TIMEOUT_SECONDS = env.int("OBJECT_STORAGE_READ_TIMEOUT_SECONDS")
OBJECT_STORAGE_VERIFY_TLS = env.bool("OBJECT_STORAGE_VERIFY_TLS")
TEMP_STORAGE_TTL_HOURS = env.int("TEMP_STORAGE_TTL_HOURS")
TEMP_STORAGE_CLEANUP_ENABLED = env.bool("TEMP_STORAGE_CLEANUP_ENABLED")
TEMP_STORAGE_CLEANUP_BATCH_SIZE = env.int("TEMP_STORAGE_CLEANUP_BATCH_SIZE")
TEMP_STORAGE_CLEANUP_HOUR = env.int("TEMP_STORAGE_CLEANUP_HOUR")
TEMP_STORAGE_CLEANUP_CLAIM_TIMEOUT_MINUTES = env.int("TEMP_STORAGE_CLEANUP_CLAIM_TIMEOUT_MINUTES")
PERSISTENT_STORAGE_QUOTA_BYTES = env.int("PERSISTENT_STORAGE_QUOTA_BYTES")
PERSISTENT_STORAGE_QUOTA_ENFORCEMENT_ENABLED = env.bool(
    "PERSISTENT_STORAGE_QUOTA_ENFORCEMENT_ENABLED"
)
DIRECT_UPLOAD_ENABLED = env.bool("DIRECT_UPLOAD_ENABLED")
DIRECT_UPLOAD_STAFF_ONLY = env.bool("DIRECT_UPLOAD_STAFF_ONLY")
DIRECT_UPLOAD_URL_TTL_MINUTES = env.int("DIRECT_UPLOAD_URL_TTL_MINUTES")
DIRECT_UPLOAD_SESSION_TTL_HOURS = env.int("DIRECT_UPLOAD_SESSION_TTL_HOURS")
DIRECT_UPLOAD_CLEANUP_HOUR = env.int("DIRECT_UPLOAD_CLEANUP_HOUR")
DIRECT_UPLOAD_CLEANUP_MINUTE = env.int("DIRECT_UPLOAD_CLEANUP_MINUTE")

if STORAGE_MODE != "local":
    missing_storage_settings = [
        name
        for name, value in {
            "OBJECT_STORAGE_ENDPOINT": OBJECT_STORAGE_ENDPOINT,
            "OBJECT_STORAGE_REGION": OBJECT_STORAGE_REGION,
            "OBJECT_STORAGE_BUCKET": OBJECT_STORAGE_BUCKET,
            "OBJECT_STORAGE_ACCESS_KEY": OBJECT_STORAGE_ACCESS_KEY,
            "OBJECT_STORAGE_SECRET_KEY": OBJECT_STORAGE_SECRET_KEY,
        }.items()
        if not value
    ]
    if missing_storage_settings:
        raise ImproperlyConfigured(
            "Object storage is enabled but required settings are missing: " + ", ".join(missing_storage_settings)
        )

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "rest_framework_simplejwt.token_blacklist",
    "django_celery_beat",
    "apps.accounts",
    "apps.common",
    "apps.api",
    "apps.dashboard",
    "apps.lectures",
    "apps.uploads",
    "apps.notes",
    "apps.referrals",
    "apps.messaging",
    "apps.ai_pipeline",
    "apps.audio_pipeline",
    "apps.subscriptions",
    "apps.usage",
    "apps.payments",
    "apps.wallet",
    "apps.support",
    "apps.admin_ops",
    "apps.ai_costs",
    "apps.storage_lifecycle",
]

MIDDLEWARE = [
    "apps.api.middleware.CdnSafeErrorMiddleware",
    "infrastructure.perf.middleware.RequestTimingMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "infrastructure.logging.middleware.RequestLoggingMiddleware",
    "apps.api.middleware.ApiErrorRecorderMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default=(
            f"postgres://{env('POSTGRES_USER')}:{env('POSTGRES_PASSWORD')}"
            f"@{env('POSTGRES_HOST')}:{env('POSTGRES_PORT')}/{env('POSTGRES_DB')}"
        ),
    )
}
database_pool_enabled = env.bool("DATABASE_POOL_ENABLED", default=True)
DATABASES["default"]["CONN_MAX_AGE"] = env.int(
    "DATABASE_CONN_MAX_AGE",
    default=0 if database_pool_enabled else 60,
)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DATABASES["default"]["OPTIONS"] = {
    "connect_timeout": env.int("DATABASE_CONNECT_TIMEOUT", default=5),
    "application_name": "tesanote-backend",
}
if database_pool_enabled:
    DATABASES["default"]["OPTIONS"]["pool"] = {
        "min_size": env.int("DATABASE_POOL_MIN_SIZE", default=1),
        "max_size": env.int("DATABASE_POOL_MAX_SIZE", default=10),
        "timeout": env.int("DATABASE_POOL_TIMEOUT", default=30),
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="UTC")
USE_I18N = True
USE_TZ = True

CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER")

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS: list[Path] = []

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
LECTURE_UPLOAD_ROOT = "lectures"

AUDIO_MAX_UPLOAD_SIZE_BYTES = env.int("AUDIO_MAX_UPLOAD_SIZE_MB") * 1024 * 1024
SLIDES_MAX_UPLOAD_SIZE_BYTES = env.int("SLIDES_MAX_UPLOAD_SIZE_MB") * 1024 * 1024
MAX_AUDIO_DURATION_MINUTES = env.int("MAX_AUDIO_DURATION_MINUTES")
AUDIO_MAX_DURATION_SECONDS = env.int("AUDIO_MAX_DURATION_SECONDS") or (
    MAX_AUDIO_DURATION_MINUTES * 60
)
ANON_UPLOAD_ENABLED = env.bool("ANON_UPLOAD_ENABLED")
ANON_UPLOAD_TTL_HOURS = env.int("ANON_UPLOAD_TTL_HOURS")
ANON_UPLOAD_MAX_PER_IP_PER_DAY = env.int("ANON_UPLOAD_MAX_PER_IP_PER_DAY")
# A new user's first note is made from the whole file whatever its length (up to the audio limit):
# the missing minutes are credited as bonus at admission. Both lines are needed (schema + assignment).
FIRST_NOTE_FULL_ENABLED = env.bool("FIRST_NOTE_FULL_ENABLED")
FIRST_NOTE_FULL_MIN_BALANCE_MINUTES = env.int("FIRST_NOTE_FULL_MIN_BALANCE_MINUTES")
ANON_UPLOAD_MAX_LIVE_UPLOADS = env.int("ANON_UPLOAD_MAX_LIVE_UPLOADS")
FIRST_PURCHASE_DISCOUNT_ENABLED = env.bool("FIRST_PURCHASE_DISCOUNT_ENABLED")
FIRST_PURCHASE_DISCOUNT_PERCENT = env.int("FIRST_PURCHASE_DISCOUNT_PERCENT")
FIRST_PURCHASE_DISCOUNT_HOURS = env.int("FIRST_PURCHASE_DISCOUNT_HOURS")
ANALYTICS_EXCLUDED_ACCOUNTS = [item.strip().lower() for item in env.list("ANALYTICS_EXCLUDED_ACCOUNTS") if item.strip()]
PERF_METRICS_ENABLED = env.bool("PERF_METRICS_ENABLED")
PERF_FLUSH_SECONDS = max(env.int("PERF_FLUSH_SECONDS"), 10)
PERF_RETENTION_DAYS = max(env.int("PERF_RETENTION_DAYS"), 1)
FFMPEG_BINARY = env("FFMPEG_BINARY")
FFPROBE_BINARY = env("FFPROBE_BINARY")
GAPGPT_API_KEY = env("GAPGPT_API_KEY")
PRIMARY_AI_API_KEY = (env("PRIMARY_AI_API_KEY") or GAPGPT_API_KEY).strip()
PRIMARY_AI_BASE_URL = env("PRIMARY_AI_BASE_URL").rstrip("/")
PRIMARY_AI_MAX_RETRIES = env.int("PRIMARY_AI_MAX_RETRIES")
PRIMARY_AI_RETRY_BASE_DELAY_SECONDS = env.float("PRIMARY_AI_RETRY_BASE_DELAY_SECONDS")
GAPGPT_WHISPER_PRICE_PER_MINUTE = env.float("GAPGPT_WHISPER_PRICE_PER_MINUTE")
GAPGPT_MAX_CLIP_DURATION_SECONDS = env.float("GAPGPT_MAX_CLIP_DURATION_SECONDS")
GAPGPT_WHISPER_MIN_JOB_COST = env.float("GAPGPT_WHISPER_MIN_JOB_COST")
GAPGPT_TRANSCRIPTION_MIN_CHUNK_SECONDS = env.float("GAPGPT_TRANSCRIPTION_MIN_CHUNK_SECONDS")
GAPGPT_TRANSCRIPTION_TIMEOUT_SECONDS = env.int("GAPGPT_TRANSCRIPTION_TIMEOUT_SECONDS")
AVANEGAR_API_TOKEN = env("AVANEGAR_API_TOKEN")
AVANEGAR_BASE_URL = env("AVANEGAR_BASE_URL").rstrip("/")
AVANEGAR_REQUEST_PATH = env("AVANEGAR_REQUEST_PATH")
AVANEGAR_REQUEST_TIMEOUT_SECONDS = env.int("AVANEGAR_REQUEST_TIMEOUT_SECONDS")
AVANEGAR_REQUEST_MAX_RETRIES = env.int("AVANEGAR_REQUEST_MAX_RETRIES")
AVANEGAR_REQUEST_RETRY_BASE_DELAY_SECONDS = env.float("AVANEGAR_REQUEST_RETRY_BASE_DELAY_SECONDS")
AVANEGAR_BILLING_UNIT_SECONDS = env.float("AVANEGAR_BILLING_UNIT_SECONDS")
AVANEGAR_TARGET_CHUNK_DURATION_SECONDS = env.float("AVANEGAR_TARGET_CHUNK_DURATION_SECONDS")
AVANEGAR_MAX_CHUNK_DURATION_SECONDS = env.float("AVANEGAR_MAX_CHUNK_DURATION_SECONDS")
DADMATECH_API_TOKEN = env("DADMATECH_API_TOKEN")
DADMATECH_BASE_URL = env("DADMATECH_BASE_URL").rstrip("/")
STT_CONTEXT_PROMPT_ENABLED = env.bool("STT_CONTEXT_PROMPT_ENABLED")
HOTWORDS_MAX_COUNT = env.int("HOTWORDS_MAX_COUNT")
HOTWORDS_MAX_LENGTH = env.int("HOTWORDS_MAX_LENGTH")
AUDIO_FAST_NORMALIZATION = env.bool("AUDIO_FAST_NORMALIZATION")
AUDIO_ENABLE_HEAVY_DENOISE = env.bool("AUDIO_ENABLE_HEAVY_DENOISE")
AUDIO_ENABLE_LOUDNORM = env.bool("AUDIO_ENABLE_LOUDNORM")
AUDIO_SEGMENTED_PIPELINE_ENABLED = env.bool("AUDIO_SEGMENTED_PIPELINE_ENABLED")
AUDIO_SEGMENTED_PIPELINE_MIN_DURATION_SECONDS = env.int(
    "AUDIO_SEGMENTED_PIPELINE_MIN_DURATION_SECONDS"
)
AUDIO_SEGMENT_SECONDS = env.int("AUDIO_SEGMENT_SECONDS")
AUDIO_SEGMENT_OVERLAP_SECONDS = env.int("AUDIO_SEGMENT_OVERLAP_SECONDS")
AUDIO_FFMPEG_THREADS = env.int("AUDIO_FFMPEG_THREADS")
AUDIO_PIPELINE_MAX_CONCURRENCY = env.int("AUDIO_PIPELINE_MAX_CONCURRENCY")
AUDIO_DELETE_TEMP_SEGMENTS_AFTER_SUCCESS = env.bool("AUDIO_DELETE_TEMP_SEGMENTS_AFTER_SUCCESS")
AUDIO_KEEP_CHUNKS_AFTER_TRANSCRIPTION = env.bool("AUDIO_KEEP_CHUNKS_AFTER_TRANSCRIPTION")
AUDIO_VAD_PADDING_SECONDS = env.float("AUDIO_VAD_PADDING_SECONDS")
AUDIO_MIN_SPEECH_RATIO_FLOOR = env.float("AUDIO_MIN_SPEECH_RATIO_FLOOR")
AUDIO_MIN_REMOVABLE_SILENCE_SECONDS = env.float("AUDIO_MIN_REMOVABLE_SILENCE_SECONDS")
TRANSCRIPTION_BATCH_SIZE = env.int("TRANSCRIPTION_BATCH_SIZE")
TRANSCRIPTION_SUBMIT_PARALLELISM = env.int("TRANSCRIPTION_SUBMIT_PARALLELISM")
TRANSCRIPTION_RECOVERY_ENABLED = env.bool("TRANSCRIPTION_RECOVERY_ENABLED")
TRANSCRIPTION_STUCK_TIMEOUT_SECONDS = env.int("TRANSCRIPTION_STUCK_TIMEOUT_SECONDS")
TRANSCRIPTION_SUBMISSION_STALE_SECONDS = env.int("TRANSCRIPTION_SUBMISSION_STALE_SECONDS")
NOTE_LLM_BASE_URL = env("NOTE_LLM_BASE_URL").rstrip("/")
NOTE_PRIMARY_PROVIDER = env("NOTE_PRIMARY_PROVIDER").strip().lower()
NOTE_PRIMARY_MODEL = env("NOTE_PRIMARY_MODEL")
NOTE_FALLBACK_PROVIDER = env("NOTE_FALLBACK_PROVIDER").strip().lower()
NOTE_FALLBACK_MODEL = env("NOTE_FALLBACK_MODEL")
NOTE_AB_TEST_MODELS = env.list("NOTE_AB_TEST_MODELS")
NOTE_LLM_TEMPERATURE = env.float("NOTE_LLM_TEMPERATURE")
NOTE_LLM_MAX_RETRIES = env.int("NOTE_LLM_MAX_RETRIES")
NOTE_LLM_RETRY_BASE_DELAY_SECONDS = env.float("NOTE_LLM_RETRY_BASE_DELAY_SECONDS")
NOTE_LLM_TIMEOUT_SECONDS = env.int("NOTE_LLM_TIMEOUT_SECONDS")
NOTE_LLM_MAX_OUTPUT_TOKENS = env.int("NOTE_LLM_MAX_OUTPUT_TOKENS")
NOTE_FALLBACK_MODELS = env.list("NOTE_FALLBACK_MODELS")
NOTE_GENERATION_V2_ENABLED = env.bool("NOTE_GENERATION_V2_ENABLED")
NOTE_GENERATION_V2_MODEL = env("NOTE_GENERATION_V2_MODEL")
NOTE_GENERATION_V3_ENABLED = env.bool("NOTE_GENERATION_V3_ENABLED")
NOTE_GENERATION_V3_MODEL = env("NOTE_GENERATION_V3_MODEL")
NOTE_GENERATION_V3_USER_IDS = env.list("NOTE_GENERATION_V3_USER_IDS")
NOTE_GENERATION_V3_FOR_ALL = env.bool("NOTE_GENERATION_V3_FOR_ALL")
NOTE_GENERATION_V4_ENABLED = env.bool("NOTE_GENERATION_V4_ENABLED")
NOTE_GENERATION_V4_FOR_ALL = env.bool("NOTE_GENERATION_V4_FOR_ALL")
NOTE_GENERATION_V4_PHONE_NUMBERS = env.list("NOTE_GENERATION_V4_PHONE_NUMBERS")
NOTE_COMPARISON_V2_ENABLED = env.bool("NOTE_COMPARISON_V2_ENABLED")
NOTE_COMPARISON_V2_PHONE_NUMBERS = env.list("NOTE_COMPARISON_V2_PHONE_NUMBERS")
NOTE_QUALITY_V21_ENABLED = env.bool("NOTE_QUALITY_V21_ENABLED")
NOTE_QUALITY_V21_PHONE_NUMBERS = env.list("NOTE_QUALITY_V21_PHONE_NUMBERS")
NOTE_QUALITY_V42_ENABLED = env.bool("NOTE_QUALITY_V42_ENABLED")
NOTE_QUALITY_V42_PHONE_NUMBERS = env.list("NOTE_QUALITY_V42_PHONE_NUMBERS")
NOTE_QUALITY_V42_FOR_ALL = env.bool("NOTE_QUALITY_V42_FOR_ALL")
NOTE_QUALITY_V42_GUARDS_ENABLED = env.bool("NOTE_QUALITY_V42_GUARDS_ENABLED")
NOTE_QUALITY_V42_TERM_GUARD = env("NOTE_QUALITY_V42_TERM_GUARD")
NOTE_QUALITY_V42_CHUNK_MARKS_ENABLED = env.bool("NOTE_QUALITY_V42_CHUNK_MARKS_ENABLED")
NOTE_QUALITY_V41_FOR_ALL = env.bool("NOTE_QUALITY_V41_FOR_ALL")
CLIENT_COUNTRY_HEADERS = env.list("CLIENT_COUNTRY_HEADERS")
CLIENT_GEOIP_COUNTRY_DB = env("CLIENT_GEOIP_COUNTRY_DB")
NOTE_V3_DETAILED_SOURCE_RATIO = env.float("NOTE_V3_DETAILED_SOURCE_RATIO")
NOTE_V3_SUMMARIZED_SOURCE_RATIO = env.float("NOTE_V3_SUMMARIZED_SOURCE_RATIO")
NOTE_V3_MAX_NOTE_CHARS = env.int("NOTE_V3_MAX_NOTE_CHARS")
NOTE_V3_MAX_SUMMARIZED_NOTE_CHARS = env.int("NOTE_V3_MAX_SUMMARIZED_NOTE_CHARS")
NOTE_V3_MIN_NOTE_CHARS = env.int("NOTE_V3_MIN_NOTE_CHARS")
NOTE_V3_BUDGET_TOLERANCE = env.float("NOTE_V3_BUDGET_TOLERANCE")
NOTE_V3_ENFORCE_BUDGET = env.bool("NOTE_V3_ENFORCE_BUDGET")
NOTE_V3_MIN_FILL_RATIO = env.float("NOTE_V3_MIN_FILL_RATIO")
NOTE_V3_EXPAND_THIN_TOPICS = env.bool("NOTE_V3_EXPAND_THIN_TOPICS")
CHAT_MODEL_PROVIDER = env("CHAT_MODEL_PROVIDER").strip().lower()
CHAT_MODEL_NAME = env("CHAT_MODEL_NAME").strip()
CHAT_MODEL_MAX_OUTPUT_TOKENS = env.int("CHAT_MODEL_MAX_OUTPUT_TOKENS")
CHAT_MODEL_TEMPERATURE = env.float("CHAT_MODEL_TEMPERATURE")
CHAT_QUOTA_ENABLED = env.bool("CHAT_QUOTA_ENABLED")
CHAT_MONTHLY_COST_CAP_USD = env("CHAT_MONTHLY_COST_CAP_USD").strip()
CHAT_5_HOUR_COST_CAP_USD = env("CHAT_5_HOUR_COST_CAP_USD").strip()
CHAT_WEEKLY_COST_CAP_USD = env("CHAT_WEEKLY_COST_CAP_USD").strip()
# Direct Groq remains an explicit opt-in. The default tutor route uses
# AVALAI_API_KEY with AvalAI's unified API instead.
GROQ_API_KEY = env("GROQ_API_KEY").strip()
CHAT_GROQ_API_KEY = env("CHAT_GROQ_API_KEY").strip()
CHAT_MODEL_BASE_URL = env("CHAT_MODEL_BASE_URL").rstrip("/")
CHAT_MODEL_TIMEOUT_SECONDS = env.float("CHAT_MODEL_TIMEOUT_SECONDS")
CHAT_MODEL_MAX_RETRIES = env.int("CHAT_MODEL_MAX_RETRIES")
# Emergency mode (international internet down): only Iran-hosted AI. DeepSeek on GapGPT is the
# cheaper primary, Qwen3.5 on Arvan the fallback; the tutor chat uses the cheap Persian Xerxes-1.
EMERGENCY_AI_PRIMARY_PROVIDER = env("EMERGENCY_AI_PRIMARY_PROVIDER").strip().lower()
EMERGENCY_AI_PRIMARY_MODEL = env("EMERGENCY_AI_PRIMARY_MODEL").strip()
EMERGENCY_AI_FALLBACK_PROVIDER = env("EMERGENCY_AI_FALLBACK_PROVIDER").strip().lower()
EMERGENCY_AI_FALLBACK_MODEL = env("EMERGENCY_AI_FALLBACK_MODEL").strip()
EMERGENCY_CHAT_PROVIDER = env("EMERGENCY_CHAT_PROVIDER").strip().lower()
EMERGENCY_CHAT_MODEL = env("EMERGENCY_CHAT_MODEL").strip()
EMERGENCY_CHAT_FALLBACK_PROVIDER = env("EMERGENCY_CHAT_FALLBACK_PROVIDER").strip().lower()
EMERGENCY_CHAT_FALLBACK_MODEL = env("EMERGENCY_CHAT_FALLBACK_MODEL").strip()
# Note generation from pasted text is charged in minutes (see apps/notes/text_billing.py).
TEXT_NOTE_MIN_MINUTES = env.int("TEXT_NOTE_MIN_MINUTES")
TEXT_NOTE_MAX_CHARS = env.int("TEXT_NOTE_MAX_CHARS")
# ArvanCloud AI gateway: Iran-hosted models (Qwen3.5-35B-A3B) that survive an international blackout.
ARVAN_AI_API_KEY = env("ARVAN_AI_API_KEY").strip()
ARVAN_AI_BASE_URL = env("ARVAN_AI_BASE_URL").rstrip("/")
ARVAN_AI_MODEL = env("ARVAN_AI_MODEL").strip() or "Qwen3.5-35B-A3B"
ARVAN_AI_TIMEOUT_SECONDS = env.int("ARVAN_AI_TIMEOUT_SECONDS")
ARVAN_AI_MAX_RETRIES = env.int("ARVAN_AI_MAX_RETRIES")
ARVAN_AI_RETRY_BASE_DELAY_SECONDS = env.float("ARVAN_AI_RETRY_BASE_DELAY_SECONDS")
ARVAN_AI_PROVIDER_MAX_CONCURRENCY = env.int("ARVAN_AI_PROVIDER_MAX_CONCURRENCY")
ARVAN_AI_PROVIDER_SLOT_WAIT_SECONDS = env.int("ARVAN_AI_PROVIDER_SLOT_WAIT_SECONDS")
ARVAN_AI_PROVIDER_SLOT_POLL_SECONDS = env.float("ARVAN_AI_PROVIDER_SLOT_POLL_SECONDS")
ARVAN_AI_RPM_LIMIT = env.int("ARVAN_AI_RPM_LIMIT")
ARVAN_AI_TPM_LIMIT = env.int("ARVAN_AI_TPM_LIMIT")
ARVAN_AI_RATE_LIMIT_WAIT_SECONDS = env.int("ARVAN_AI_RATE_LIMIT_WAIT_SECONDS")
ARVAN_AI_RATE_LIMIT_POLL_SECONDS = env.float("ARVAN_AI_RATE_LIMIT_POLL_SECONDS")
ARVAN_AI_TARGET_MAX_OUTPUT_TOKENS = env.int("ARVAN_AI_TARGET_MAX_OUTPUT_TOKENS")
ARVAN_AI_DISABLE_THINKING = env.bool("ARVAN_AI_DISABLE_THINKING")
AVALAI_API_KEY = env("AVALAI_API_KEY").strip()
AVALAI_BASE_URL = env("AVALAI_BASE_URL").rstrip("/")
AVALAI_MODEL = env("AVALAI_MODEL").strip()
AVALAI_STT_MODEL = env("AVALAI_STT_MODEL").strip()
AVALAI_STT_TIMEOUT_SECONDS = env.int("AVALAI_STT_TIMEOUT_SECONDS")
AVALAI_STT_MAX_RETRIES = env.int("AVALAI_STT_MAX_RETRIES")
AVALAI_STT_LANGUAGE = env("AVALAI_STT_LANGUAGE").strip() or "fa"
AVALAI_STT_RESPONSE_FORMAT = env("AVALAI_STT_RESPONSE_FORMAT").strip() or "text"
AVALAI_STT_TEMPERATURE = env.float("AVALAI_STT_TEMPERATURE")
AVALAI_MAX_RETRIES = env.int("AVALAI_MAX_RETRIES")
AVALAI_RETRY_BASE_DELAY_SECONDS = env.float("AVALAI_RETRY_BASE_DELAY_SECONDS")
AVALAI_TIMEOUT_SECONDS = env.int("AVALAI_TIMEOUT_SECONDS")
AVALAI_PROVIDER_MAX_CONCURRENCY = env.int("AVALAI_PROVIDER_MAX_CONCURRENCY")
AVALAI_PROVIDER_SLOT_WAIT_SECONDS = env.int("AVALAI_PROVIDER_SLOT_WAIT_SECONDS")
AVALAI_PROVIDER_SLOT_POLL_SECONDS = env.float("AVALAI_PROVIDER_SLOT_POLL_SECONDS")
AVALAI_RPM_LIMIT = env.int("AVALAI_RPM_LIMIT")
AVALAI_TPM_LIMIT = env.int("AVALAI_TPM_LIMIT")
AVALAI_RATE_LIMIT_WAIT_SECONDS = env.int("AVALAI_RATE_LIMIT_WAIT_SECONDS")
AVALAI_RATE_LIMIT_POLL_SECONDS = env.float("AVALAI_RATE_LIMIT_POLL_SECONDS")
AVALAI_TARGET_MAX_INPUT_TOKENS = env.int("AVALAI_TARGET_MAX_INPUT_TOKENS")
AVALAI_TARGET_MAX_OUTPUT_TOKENS = env.int("AVALAI_TARGET_MAX_OUTPUT_TOKENS")
AVALAI_SLIDE_CONTEXT_CHAR_LIMIT = env.int("AVALAI_SLIDE_CONTEXT_CHAR_LIMIT")
AVALAI_TRANSCRIPT_OUTLINE_CHAR_LIMIT = env.int("AVALAI_TRANSCRIPT_OUTLINE_CHAR_LIMIT")
TRANSCRIPTION_PRIMARY_PROVIDER = env("TRANSCRIPTION_PRIMARY_PROVIDER").strip()
TRANSCRIPTION_FALLBACK_PROVIDER = env("TRANSCRIPTION_FALLBACK_PROVIDER").strip()
MAX_AVALAI_STT_FILE_MB = env.int("MAX_AVALAI_STT_FILE_MB")
TRANSCRIPTION_CHUNK_TARGET_MB = env.int("TRANSCRIPTION_CHUNK_TARGET_MB")
TRANSCRIPTION_CHUNK_MAX_SECONDS = env.int("TRANSCRIPTION_CHUNK_MAX_SECONDS")
NOTE_SUMMARY_CHUNK_CHARS = env.int("NOTE_SUMMARY_CHUNK_CHARS")
NOTE_LONG_TRANSCRIPT_THRESHOLD_CHARS = env.int("NOTE_LONG_TRANSCRIPT_THRESHOLD_CHARS")
NOTE_SEMANTIC_BATCH_CHARS = env.int("NOTE_SEMANTIC_BATCH_CHARS")
# Accuracy check ("note review"): per-user gated until the founder enables it for everyone.
# Every flag needs BOTH the schema entry above and this assignment.
# Prepaid wallet billing. Gated per user (the founder tests first); every flag needs the schema entry
# above AND the assignment here.
WALLET_BILLING_ENABLED = env.bool("WALLET_BILLING_ENABLED")
WALLET_BILLING_FOR_ALL = env.bool("WALLET_BILLING_FOR_ALL")
# Wallet users see no subscription-type label; set true to show it again.
WALLET_SHOW_PLAN_LABEL = env.bool("WALLET_SHOW_PLAN_LABEL")
WALLET_BILLING_ROLLOUT_PERCENT = max(0, min(100, env.int("WALLET_BILLING_ROLLOUT_PERCENT")))
WALLET_BILLING_PHONE_NUMBERS = [phone.strip() for phone in env.list("WALLET_BILLING_PHONE_NUMBERS") if phone.strip()]
# Test numbers that are always treated as brand-new on every sign-up: deleting the account and
# registering again never counts as a "returning number" (full free allowance, no withheld minutes).
FRESH_ACCOUNT_PHONE_NUMBERS = [phone.strip() for phone in env.list("FRESH_ACCOUNT_PHONE_NUMBERS") if phone.strip()]
WALLET_PROFIT_TOMAN_PER_MINUTE = env.int("WALLET_PROFIT_TOMAN_PER_MINUTE")
WALLET_STT_TOMAN_PER_MINUTE = env.int("WALLET_STT_TOMAN_PER_MINUTE")
WALLET_HOLD_FACTOR_PERCENT = env.int("WALLET_HOLD_FACTOR_PERCENT")
WALLET_MIN_NOTE_CHARGE_TOMAN = env.int("WALLET_MIN_NOTE_CHARGE_TOMAN")
WALLET_ADDON_MARKUP_PERCENT = env.int("WALLET_ADDON_MARKUP_PERCENT")
WALLET_ADDON_MIN_BALANCE_TOMAN = env.int("WALLET_ADDON_MIN_BALANCE_TOMAN")
WALLET_FIRST_NOTE_FREE_CHAT_MESSAGES = env.int("WALLET_FIRST_NOTE_FREE_CHAT_MESSAGES")
WALLET_MIN_TOPUP_TOMAN = env.int("WALLET_MIN_TOPUP_TOMAN")
WALLET_MAX_TOPUP_TOMAN = env.int("WALLET_MAX_TOPUP_TOMAN")
WALLET_OFFER_MINUTES = env.int("WALLET_OFFER_MINUTES")
WALLET_GIFT_TIERS = env("WALLET_GIFT_TIERS")
WALLET_GIFT_EXPIRY_DAYS = env.int("WALLET_GIFT_EXPIRY_DAYS")
WALLET_LEGACY_GIFT_EXPIRY_DAYS = env.int("WALLET_LEGACY_GIFT_EXPIRY_DAYS")
# Only accounts created before this moment get their leftover free minutes turned into gift credit.
# Newer accounts get the free first note and nothing else: their leftover free minutes are cleared.
WALLET_LEGACY_CUTOFF = env("WALLET_LEGACY_CUTOFF")
NOTE_STYLE_CHOICE_ENABLED = env.bool("NOTE_STYLE_CHOICE_ENABLED")
NOTE_STYLE_CHOICE_STAFF_ONLY = env.bool("NOTE_STYLE_CHOICE_STAFF_ONLY")
NOTE_STYLE_TRANSCRIPT_PRICE_PERCENT = env.int("NOTE_STYLE_TRANSCRIPT_PRICE_PERCENT")
NOTE_REVIEW_CAPTURE_ENABLED = env.bool("NOTE_REVIEW_CAPTURE_ENABLED")
NOTE_REVIEW_FOR_ALL = env.bool("NOTE_REVIEW_FOR_ALL")
NOTE_REVIEW_MAX_SPOTS = env.int("NOTE_REVIEW_MAX_SPOTS")
NOTE_REVIEW_MAX_FLAG_RATIO = env.float("NOTE_REVIEW_MAX_FLAG_RATIO")
NOTE_REVIEW_MIN_BADNESS = env.float("NOTE_REVIEW_MIN_BADNESS")
NOTE_REVIEW_MIN_ALIGNMENT_SCORE = env.float("NOTE_REVIEW_MIN_ALIGNMENT_SCORE")
NOTE_REVIEW_CLIP_SECONDS = env.int("NOTE_REVIEW_CLIP_SECONDS")
NOTE_REVIEW_CLIP_LEAD_SECONDS = env.int("NOTE_REVIEW_CLIP_LEAD_SECONDS")
NOTE_REVIEW_CLIP_BITRATE_KBPS = env.int("NOTE_REVIEW_CLIP_BITRATE_KBPS")
NOTE_REVIEW_DURATION_TOLERANCE_SECONDS = env.float("NOTE_REVIEW_DURATION_TOLERANCE_SECONDS")
NOTE_REVIEW_CLIP_URL_TTL_SECONDS = env.int("NOTE_REVIEW_CLIP_URL_TTL_SECONDS")
NOTE_REVIEW_SHOW_PERCENT = env.bool("NOTE_REVIEW_SHOW_PERCENT")
NOTE_PDF_NODE_EXECUTABLE = env("NOTE_PDF_NODE_EXECUTABLE")
NOTE_PDF_PLAYWRIGHT_MODULE = env("NOTE_PDF_PLAYWRIGHT_MODULE")
NOTE_PDF_BROWSER_EXECUTABLE = env("NOTE_PDF_BROWSER_EXECUTABLE")
NOTE_PDF_RENDER_TIMEOUT_SECONDS = env.int("NOTE_PDF_RENDER_TIMEOUT_SECONDS")
BACKEND_PUBLIC_BASE_URL = env("BACKEND_PUBLIC_BASE_URL").rstrip("/")
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER")
PAYMENT_MOCK_ENABLED = env.bool("PAYMENT_MOCK_ENABLED")
PAYMENT_FRONTEND_BASE_URL = env("PAYMENT_FRONTEND_BASE_URL").rstrip("/")
PAYMENT_CALLBACK_PATH = env("PAYMENT_CALLBACK_PATH")
ZARINPAL_MERCHANT_ID = env("ZARINPAL_MERCHANT_ID")
ZARINPAL_REQUEST_URL = env("ZARINPAL_REQUEST_URL")
ZARINPAL_VERIFY_URL = env("ZARINPAL_VERIFY_URL")
ZARINPAL_START_PAY_BASE_URL = env("ZARINPAL_START_PAY_BASE_URL")
ZIBAL_MERCHANT = env("ZIBAL_MERCHANT").strip()
ZIBAL_REQUEST_URL = env("ZIBAL_REQUEST_URL")
ZIBAL_VERIFY_URL = env("ZIBAL_VERIFY_URL")
ZIBAL_INQUIRY_URL = env("ZIBAL_INQUIRY_URL")
ZIBAL_START_URL = env("ZIBAL_START_URL")
ZIBAL_CONNECT_TIMEOUT_SECONDS = env.float("ZIBAL_CONNECT_TIMEOUT_SECONDS")
ZIBAL_READ_TIMEOUT_SECONDS = env.float("ZIBAL_READ_TIMEOUT_SECONDS")
ZIBAL_VERIFY_MAX_RETRIES = env.int("ZIBAL_VERIFY_MAX_RETRIES")
PAYMENT_SESSION_TTL_MINUTES = env.int("PAYMENT_SESSION_TTL_MINUTES")
PAYMENT_RECONCILE_STALE_SECONDS = env.int("PAYMENT_RECONCILE_STALE_SECONDS")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

CORS_ALLOWED_ORIGINS = env.list("DJANGO_CORS_ALLOWED_ORIGINS")
CORS_ALLOW_CREDENTIALS = True
CORS_EXPOSE_HEADERS = ["X-TN-Status", "X-Request-ID"]
CORS_ALLOW_HEADERS = [*default_headers, "x-tn-envelope"]
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS")

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.api.pagination.DefaultPageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_VERSION": "v1",
    "ALLOWED_VERSIONS": ("v1",),
    "DEFAULT_THROTTLE_RATES": {
        "payment_session": "10/min",
        "payment_callback": "120/min",
        "product_analytics": "120/min",
        "perf_beacon": "30/min",
        "client_error_report": "30/min",
        "note_feedback": "60/hour",
        "share_preview": "120/min",
        "share_link": "30/hour",
        "share_claim": "60/hour",
        "study_items_generation": "5/hour",
        "note_summary_generation": "6/hour",
        "note_image_upload": "30/hour",
        "upload_telemetry": "1200/hour",
        "system_status": "600/hour",
        "anonymous_upload_start": "30/hour",
        "anonymous_upload_chunk": "1500/hour",
        "anonymous_upload_complete": "60/hour",
    },
    "EXCEPTION_HANDLER": "apps.api.exceptions.custom_exception_handler",
}

# Referral gifts. The owner of a shared note earns nothing for a plain signup
# (REFERRAL_REWARD_MINUTES) and REFERRAL_PURCHASE_REWARD_MINUTES once the invited friend buys a
# plan; the invited friend gets REFERRAL_FRIEND_BONUS_MINUTES on top of the free plan at signup.
REFERRAL_REWARD_MINUTES = env.int("REFERRAL_REWARD_MINUTES", default=0)
REFERRAL_PURCHASE_REWARD_MINUTES = env.int("REFERRAL_PURCHASE_REWARD_MINUTES", default=150)
REFERRAL_FRIEND_BONUS_MINUTES = env.int("REFERRAL_FRIEND_BONUS_MINUTES", default=30)
# What 60 minutes of lecture audio really costs us (transcription + note + study tools).
# Plan margins and profit estimates are computed from this and each plan's own minutes.
REAL_COST_PER_60_MINUTES_TOMAN = env.int("REAL_COST_PER_60_MINUTES_TOMAN", default=12000)
REFERRAL_MONTHLY_CAP_MINUTES = env.int("REFERRAL_MONTHLY_CAP_MINUTES", default=1200)
SHARE_PREVIEW_PAGES = env.int("SHARE_PREVIEW_PAGES", default=2)

ACCESS_TOKEN_LIFETIME_MINUTES = env.int("ACCESS_TOKEN_LIFETIME_MINUTES")
ACCESS_TOKEN_LIFETIME_DAYS = env.int("ACCESS_TOKEN_LIFETIME_DAYS")
REFRESH_TOKEN_LIFETIME_DAYS = env.int("REFRESH_TOKEN_LIFETIME_DAYS")
JWT_REFRESH_COOKIE_NAME = env("JWT_REFRESH_COOKIE_NAME")
JWT_REFRESH_COOKIE_PATH = env("JWT_REFRESH_COOKIE_PATH")
JWT_REFRESH_COOKIE_SECURE = env.bool("JWT_REFRESH_COOKIE_SECURE")
JWT_REFRESH_COOKIE_SAMESITE = env("JWT_REFRESH_COOKIE_SAMESITE")
JWT_REFRESH_COOKIE_DOMAIN = env("JWT_REFRESH_COOKIE_DOMAIN")
JWT_REFRESH_COOKIE_MAX_AGE_SECONDS = REFRESH_TOKEN_LIFETIME_DAYS * 24 * 60 * 60
MELIPAYAMAK_USERNAME = env("MELIPAYAMAK_USERNAME").strip()
MELIPAYAMAK_API_KEY = env("MELIPAYAMAK_API_KEY").strip()
MELIPAYAMAK_BODY_ID = env.int("MELIPAYAMAK_BODY_ID")
MELIPAYAMAK_BASE_URL = env("MELIPAYAMAK_BASE_URL").rstrip("/")
MELIPAYAMAK_BODY_ID_NOTE_READY = env.int("MELIPAYAMAK_BODY_ID_NOTE_READY")
MELIPAYAMAK_SENDER_NUMBER = env("MELIPAYAMAK_SENDER_NUMBER").strip()
MELIPAYAMAK_TEXT_URL = env("MELIPAYAMAK_TEXT_URL").strip()
# The win-back text (no pattern, from our dedicated line). Off until the founder approves the test.
SMS_WINBACK_ENABLED = env.bool("SMS_WINBACK_ENABLED")
SMS_WINBACK_MIN_AGE_HOURS = env.int("SMS_WINBACK_MIN_AGE_HOURS")
SMS_WINBACK_TEST_NUMBERS = [phone.strip() for phone in env.list("SMS_WINBACK_TEST_NUMBERS") if phone.strip()]
SMS_AUTOMATION_ENABLED = env.bool("SMS_AUTOMATION_ENABLED")
SMS_AUTOMATION_DRY_RUN = env.bool("SMS_AUTOMATION_DRY_RUN")
SMS_NOTE_READY_ENABLED = env.bool("SMS_NOTE_READY_ENABLED")
SMS_NOTE_READY_DELAY_MINUTES = env.int("SMS_NOTE_READY_DELAY_MINUTES")
SMS_NOTE_READY_MAX_AGE_HOURS = env.int("SMS_NOTE_READY_MAX_AGE_HOURS")
SMS_HOLDOUT_PERCENT = env.int("SMS_HOLDOUT_PERCENT")
SMS_MAX_PER_HOUR = env.int("SMS_MAX_PER_HOUR")
SMS_MONTHLY_BUDGET_TOMAN = env.int("SMS_MONTHLY_BUDGET_TOMAN")
SMS_COST_PER_PART_TOMAN = env.int("SMS_COST_PER_PART_TOMAN")
SMS_NOTE_READY_PARTS = env.int("SMS_NOTE_READY_PARTS")
SMS_TEST_ALLOWLIST = [phone.strip() for phone in env.list("SMS_TEST_ALLOWLIST") if phone.strip()]
OTP_LENGTH = env.int("OTP_LENGTH")
OTP_EXPIRY_SECONDS = env.int("OTP_EXPIRY_SECONDS")
OTP_RESEND_COOLDOWN_SECONDS = env.int("OTP_RESEND_COOLDOWN_SECONDS")
OTP_MAX_REQUESTS_PER_HOUR = env.int("OTP_MAX_REQUESTS_PER_HOUR")
OTP_MAX_VERIFY_ATTEMPTS = env.int("OTP_MAX_VERIFY_ATTEMPTS")
OTP_LOCK_SECONDS = env.int("OTP_LOCK_SECONDS")

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(days=ACCESS_TOKEN_LIFETIME_DAYS)
    if ACCESS_TOKEN_LIFETIME_DAYS > 0
    else timedelta(minutes=ACCESS_TOKEN_LIFETIME_MINUTES),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Tesanote API",
    "DESCRIPTION": "Backend infrastructure foundation for Tesanote AI SaaS.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

REDIS_URL = env("REDIS_URL")
HEALTH_CHECK_REDIS_ENABLED = env.bool("HEALTH_CHECK_REDIS_ENABLED", default=True)

CELERY_BROKER_URL = env("CELERY_BROKER_URL")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND")
CELERY_TASK_DEFAULT_QUEUE = env("CELERY_TASK_DEFAULT_QUEUE")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_CREATE_MISSING_QUEUES = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 25 * 60
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_WORKER_PREFETCH_MULTIPLIER = env.int("CELERY_WORKER_PREFETCH_MULTIPLIER")
CELERY_WORKER_MAX_TASKS_PER_CHILD = env.int("CELERY_WORKER_MAX_TASKS_PER_CHILD")
CELERY_BROKER_POOL_LIMIT = env.int("CELERY_BROKER_POOL_LIMIT")
CELERY_RESULT_EXPIRES = env.int("CELERY_RESULT_EXPIRES_SECONDS")
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "visibility_timeout": env.int("CELERY_BROKER_VISIBILITY_TIMEOUT_SECONDS"),
}
FAIR_SCHEDULER_ENABLED = env.bool("FAIR_SCHEDULER_ENABLED", default=False)
PROCESSING_MAX_CONCURRENT_JOBS = env.int("PROCESSING_MAX_CONCURRENT_JOBS", default=1)
DYNAMIC_CONCURRENCY_ENABLED = env.bool("DYNAMIC_CONCURRENCY_ENABLED", default=False)
PROCESSING_CONCURRENCY_MIN = env.int("PROCESSING_CONCURRENCY_MIN", default=1)
PROCESSING_CONCURRENCY_MAX = env.int("PROCESSING_CONCURRENCY_MAX", default=2)
PROCESSING_CONCURRENCY_HARD_MAX = env.int("PROCESSING_CONCURRENCY_HARD_MAX", default=2)
# Phase 17: an architectural sanity ceiling independent of the operator-facing
# PROCESSING_CONCURRENCY_HARD_MAX lever -- no configuration value can exceed
# this, so a typo cannot become live capacity. Raising PROCESSING_CONCURRENCY_HARD_MAX
# from its default of 2 requires an explicit, deliberate config change; this
# setting only bounds how high that lever can ever go. Default (8) is a
# conservative single-node sanity bound, not a claim that 8 is validated safe.
PROCESSING_CONCURRENCY_ABSOLUTE_MAX = env.int("PROCESSING_CONCURRENCY_ABSOLUTE_MAX", default=8)
RESOURCE_SAMPLE_INTERVAL_SECONDS = env.int("RESOURCE_SAMPLE_INTERVAL_SECONDS", default=30)
RESOURCE_SAMPLE_MAX_AGE_SECONDS = env.int("RESOURCE_SAMPLE_MAX_AGE_SECONDS", default=90)
CONCURRENCY_SCALE_UP_STABLE_SECONDS = env.int(
    "CONCURRENCY_SCALE_UP_STABLE_SECONDS", default=120
)
CPU_SCALE_UP_THRESHOLD = env.float("CPU_SCALE_UP_THRESHOLD", default=0.60)
MEMORY_SCALE_UP_MIN_AVAILABLE_BYTES = env.int(
    "MEMORY_SCALE_UP_MIN_AVAILABLE_BYTES", default=1_000_000_000
)
SWAP_SCALE_UP_MAX_USED_BYTES = env.int("SWAP_SCALE_UP_MAX_USED_BYTES", default=256_000_000)
DISK_SAFETY_MIN_FREE_BYTES = env.int("DISK_SAFETY_MIN_FREE_BYTES", default=2_000_000_000)
FAIR_SCHEDULER_CLAIM_TIMEOUT_SECONDS = env.int(
    "FAIR_SCHEDULER_CLAIM_TIMEOUT_SECONDS", default=300
)
FAIR_SCHEDULER_SAFETY_TICK_SECONDS = env.int(
    "FAIR_SCHEDULER_SAFETY_TICK_SECONDS", default=60
)
# Phase 12: heartbeat/recovery for jobs that reached PROCESSING and went
# quiet. Interval << stale threshold gives ~15 tolerated missed ticks before
# a job is even considered a candidate (DB hiccups/GC pauses do not trip
# this); stale threshold is comfortably above the longest known single
# blocking call without an internal checkpoint (AVALAI_STT_TIMEOUT_SECONDS,
# 180s) and far below the global Celery task time limit
# (CELERY_TASK_TIME_LIMIT, 1800s) that would otherwise be the only signal.
PROCESSING_HEARTBEAT_ENABLED = env.bool("PROCESSING_HEARTBEAT_ENABLED", default=True)
PROCESSING_HEARTBEAT_INTERVAL_SECONDS = env.int(
    "PROCESSING_HEARTBEAT_INTERVAL_SECONDS", default=20
)
PROCESSING_HEARTBEAT_STALE_SECONDS = env.int(
    "PROCESSING_HEARTBEAT_STALE_SECONDS", default=300
)
# Kept disabled until fresh production heartbeat evidence is reviewed — see
# docs/scaling rollout notes. Heartbeat collection alone is safe to enable
# immediately since it only ever writes last_heartbeat_at.
PROCESSING_RECOVERY_ENABLED = env.bool("PROCESSING_RECOVERY_ENABLED", default=False)
PROCESSING_MAX_RECOVERY_ATTEMPTS = env.int("PROCESSING_MAX_RECOVERY_ATTEMPTS", default=2)
PROCESSING_RECOVERY_SCAN_BATCH_SIZE = env.int(
    "PROCESSING_RECOVERY_SCAN_BATCH_SIZE", default=20
)
# Phase 16: distributed processing-capacity foundation. PROCESSING_WORKER_HOST_ID
# is this process's own identity (used when it samples/reports its own
# resources); PROCESSING_WORKER_HOST_IDS is the registry of every currently
# registered processing-capable host, used to (a) generate one Beat
# resource-sample/heartbeat entry per host and (b) know which hosts to
# aggregate when computing distributed capacity. Adding a host is adding one
# entry to this list, not redesigning scheduling. DISTRIBUTED_CAPACITY_ENABLED
# is the master gate: while False, FairSchedulerService uses the original
# single-host DynamicCapacityController unchanged (zero behavior change,
# matches the existing production posture). PROCESSING_GLOBAL_MAX_SLOTS is an
# emergency ceiling independent of how many hosts are registered or how
# capable they are — the architecture may support many hosts; this setting
# is what actually bounds production regardless. PROCESSING_HOST_MAX_SLOTS is
# the equivalent per-host ceiling (mirrors PROCESSING_CONCURRENCY_HARD_MAX,
# but applied per host rather than globally).
PROCESSING_WORKER_HOST_ID = env("PROCESSING_WORKER_HOST_ID")
PROCESSING_WORKER_HOST_IDS = env.list("PROCESSING_WORKER_HOST_IDS")
DISTRIBUTED_CAPACITY_ENABLED = env.bool("DISTRIBUTED_CAPACITY_ENABLED", default=False)
PROCESSING_GLOBAL_MAX_SLOTS = env.int("PROCESSING_GLOBAL_MAX_SLOTS", default=2)
PROCESSING_HOST_MAX_SLOTS = env.int("PROCESSING_HOST_MAX_SLOTS", default=2)
# Phase 13: cheap per-queue worker liveness (see infrastructure.observability
# .worker_health). Ping interval well under the stale threshold so a single
# missed Beat tick does not flip a healthy worker to "stale".
WORKER_HEALTH_PING_INTERVAL_SECONDS = env.int(
    "WORKER_HEALTH_PING_INTERVAL_SECONDS", default=30
)
WORKER_HEALTH_STALE_SECONDS = env.int("WORKER_HEALTH_STALE_SECONDS", default=90)
# Phase 18: real per-stage execution concurrency (apps.ai_pipeline.stage_metrics).
# An entry older than this is treated as an orphan from a worker that died
# mid-task and is pruned on read, so the count can never leak upward forever.
# Default is generous relative to observed per-stage task durations so a
# slow-but-healthy task is never mistaken for a leak.
STAGE_CONCURRENCY_STALE_SECONDS = env.int("STAGE_CONCURRENCY_STALE_SECONDS", default=600)
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_BEAT_SCHEDULE = {
    "purge-deleted-notes": {
        "task": "apps.notes.tasks.purge_deleted_notes",
        "schedule": 3600,
    },
    "sms-note-ready": {
        "task": "apps.messaging.tasks.send_note_ready_sms",
        "schedule": 300,
    },
    "sample-processing-capacity-resources": {
        "task": "apps.ai_pipeline.tasks.sample_processing_capacity_resources",
        "schedule": RESOURCE_SAMPLE_INTERVAL_SECONDS,
    },
    "fair-scheduler-safety-tick": {
        "task": "apps.ai_pipeline.tasks.dispatch_available_processing_jobs",
        "schedule": FAIR_SCHEDULER_SAFETY_TICK_SECONDS,
    },
    "cancel-timed-out-lectures": {
        "task": "apps.ai_pipeline.tasks.cancel_timed_out_lectures",
        "schedule": 300.0,
    },
    "recover-stalled-transcriptions": {
        "task": "apps.ai_pipeline.tasks.recover_stalled_transcriptions",
        "schedule": 120.0,
    },
    "check-server-clock": {
        "task": "apps.admin_ops.tasks.check_server_clock",
        "schedule": 1800.0,
    },
    "recover-stalled-processing-jobs": {
        "task": "apps.ai_pipeline.tasks.recover_stalled_processing_jobs",
        "schedule": 60.0,
    },
    "worker-heartbeat-pipeline": {
        "task": "apps.admin_ops.tasks.emit_worker_heartbeat",
        "schedule": WORKER_HEALTH_PING_INTERVAL_SECONDS,
        "args": ("pipeline",),
        "options": {"queue": "pipeline"},
    },
    "worker-heartbeat-transcription": {
        "task": "apps.admin_ops.tasks.emit_worker_heartbeat",
        "schedule": WORKER_HEALTH_PING_INTERVAL_SECONDS,
        "args": ("transcription",),
        "options": {"queue": "transcription"},
    },
    "worker-heartbeat-notes": {
        "task": "apps.admin_ops.tasks.emit_worker_heartbeat",
        "schedule": WORKER_HEALTH_PING_INTERVAL_SECONDS,
        "args": ("notes",),
        "options": {"queue": "notes"},
    },
    "worker-heartbeat-payments": {
        "task": "apps.admin_ops.tasks.emit_worker_heartbeat",
        "schedule": WORKER_HEALTH_PING_INTERVAL_SECONDS,
        "args": ("payments",),
        "options": {"queue": "payments"},
    },
    "reset-due-subscriptions": {
        "task": "apps.subscriptions.tasks.reset_due_subscriptions",
        "schedule": 3600.0,
    },
    "reconcile-zibal-payments": {
        "task": "apps.payments.tasks.reconcile_zibal_payments",
        "schedule": 120.0,
    },
    "purge-old-api-error-events": {
        "task": "apps.admin_ops.tasks.purge_old_api_error_events",
        "schedule": 86400.0,
    },
    "prune-expired-processing-errors": {
        "task": "apps.admin_ops.tasks.prune_expired_processing_errors",
        "schedule": 86400.0,
    },
    "reconcile-terminal-processing-jobs": {
        "task": "apps.admin_ops.tasks.reconcile_terminal_processing_jobs",
        "schedule": 300.0,
    },
    "recover-stalled-note-summaries": {
        "task": "apps.notes.tasks.recover_stalled_note_summaries",
        "schedule": 60.0,
    },
    "reconcile-avalai-ai-costs": {
        "task": "apps.ai_costs.tasks.reconcile_avalai_costs",
        "schedule": TzAwareCrontab(hour=3, minute=0, tz=IRAN_BUSINESS_TIME_ZONE),
    },
    "cleanup-expired-temporary-storage": {
        "task": "apps.storage_lifecycle.tasks.cleanup_expired_temporary_objects",
        # Every two hours (it used to be once a day at TEMP_STORAGE_CLEANUP_HOUR, which let
        # the expired backlog fill the bucket). The task drains until nothing is left.
        "schedule": 2 * 60 * 60.0,
    },
    "expire-wallet-gifts": {
        "task": "apps.payments.tasks.expire_wallet_gifts",
        "schedule": 3600.0,
    },
    "record-expired-first-purchase-discounts": {
        "task": "apps.payments.tasks.record_expired_first_purchase_discounts",
        "schedule": TzAwareCrontab(minute="*/15", tz=IRAN_BUSINESS_TIME_ZONE),
    },
    "purge-expired-anonymous-uploads": {
        "task": "apps.uploads.tasks.purge_expired_anonymous_uploads",
        "schedule": TzAwareCrontab(minute=17, tz=IRAN_BUSINESS_TIME_ZONE),
    },
    "cleanup-expired-direct-uploads": {
        "task": "apps.uploads.tasks.cleanup_expired_direct_upload_sessions",
        "schedule": TzAwareCrontab(
            hour=DIRECT_UPLOAD_CLEANUP_HOUR,
            minute=DIRECT_UPLOAD_CLEANUP_MINUTE,
            tz=IRAN_BUSINESS_TIME_ZONE,
        ),
    },
}
def _build_worker_host_beat_entries(host_ids: list[str]) -> dict[str, dict[str, object]]:
    """Phase 16: one resource-sample + one pipeline-heartbeat Beat entry per
    ADDITIONAL registered worker host (the original web host keeps using the
    pre-existing "sample-processing-capacity-resources" /
    "worker-heartbeat-pipeline" entries above unchanged). Adding a worker
    host is adding its id to PROCESSING_WORKER_HOST_IDS in settings/env — no
    schedule redesign. Both tasks already exist
    (sample_processing_capacity_resources reports on whichever host's
    PROCESSING_WORKER_HOST_ID executes it; emit_worker_heartbeat just needs a
    queue name), so this only needs routing, not new task code. An empty
    PROCESSING_WORKER_HOST_IDS beyond the default ["web-local"] means this
    returns nothing — zero behavior change until a host is registered.
    """
    entries: dict[str, dict[str, object]] = {}
    for host_id in host_ids:
        if host_id == "web-local":
            continue
        entries[f"sample-worker-resources-{host_id}"] = {
            "task": "apps.ai_pipeline.tasks.sample_processing_capacity_resources",
            "schedule": RESOURCE_SAMPLE_INTERVAL_SECONDS,
            "options": {"queue": f"resources-{host_id}"},
        }
        entries[f"worker-heartbeat-pipeline-{host_id}"] = {
            "task": "apps.admin_ops.tasks.emit_worker_heartbeat",
            "schedule": WORKER_HEALTH_PING_INTERVAL_SECONDS,
            "args": (f"pipeline-{host_id}",),
            "options": {"queue": f"pipeline-{host_id}"},
        }
    return entries


CELERY_BEAT_SCHEDULE.update(_build_worker_host_beat_entries(PROCESSING_WORKER_HOST_IDS))
CELERY_TASK_ROUTES = {
    "apps.notes.tasks.purge_deleted_notes": {"queue": "default"},
    "apps.ai_pipeline.tasks.sample_processing_capacity_resources": {"queue": "default"},
    "apps.ai_pipeline.tasks.dispatch_available_processing_jobs": {"queue": "default"},
    "apps.ai_pipeline.tasks.process_lecture_pipeline": {"queue": "pipeline"},
    "apps.ai_pipeline.tasks.process_slides_pipeline": {"queue": "slides"},
    "apps.ai_pipeline.tasks.transcribe_chunks_batch": {"queue": "transcription"},
    "apps.ai_pipeline.tasks.poll_transcription_chunks_batch": {"queue": "transcription"},
    "apps.ai_pipeline.tasks.cancel_timed_out_lectures": {"queue": "pipeline"},
    "apps.ai_pipeline.tasks.recover_stalled_transcriptions": {"queue": "pipeline"},
    "apps.ai_pipeline.tasks.recover_stalled_processing_jobs": {"queue": "pipeline"},
    "apps.admin_ops.tasks.emit_worker_heartbeat": {"queue": "pipeline"},
    "apps.ai_pipeline.tasks.ensure_transcription_started": {"queue": "pipeline"},
    "apps.audio_pipeline.tasks.process_audio_pipeline": {"queue": "pipeline"},
    "apps.audio_pipeline.tasks.transcribe_chunk": {"queue": "transcription"},
    "apps.messaging.tasks.send_note_ready_sms": {"queue": "default"},
    "apps.notes.tasks.generate_note_task": {"queue": "notes"},
    "apps.notes.tasks.generate_note_from_uploaded_transcript_task": {"queue": "notes"},
    "apps.notes.tasks.generate_note_pdf_task": {"queue": "notes"},
    "apps.notes.tasks.compute_note_review_task": {"queue": "default"},
    "apps.notes.tasks.generate_comparison_v2_note_task": {"queue": "notes"},
    "apps.notes.tasks.generate_note_version_task": {"queue": "notes"},
    "apps.notes.tasks.generate_note_summary_task": {"queue": "notes"},
    "apps.notes.tasks.generate_selected_note_summary_task": {"queue": "notes"},
    "apps.notes.tasks.generate_selected_study_items_task": {"queue": "notes"},
    "apps.notes.tasks.recover_stalled_note_summaries": {"queue": "notes"},
    "apps.subscriptions.tasks.reset_due_subscriptions": {"queue": "default"},
    "apps.payments.tasks.reconcile_zibal_payments": {"queue": "payments"},
    "apps.admin_ops.tasks.prune_expired_processing_errors": {"queue": "default"},
    "apps.admin_ops.tasks.purge_old_api_error_events": {"queue": "default"},
    "apps.admin_ops.tasks.reconcile_terminal_processing_jobs": {"queue": "default"},
    "apps.ai_costs.tasks.reconcile_avalai_costs": {"queue": "default"},
    "apps.storage_lifecycle.tasks.cleanup_expired_temporary_objects": {"queue": "default"},
    "apps.uploads.tasks.cleanup_expired_direct_upload_sessions": {"queue": "default"},
    "apps.uploads.tasks.purge_expired_anonymous_uploads": {"queue": "default"},
    "apps.payments.tasks.record_expired_first_purchase_discounts": {"queue": "default"},
    "apps.payments.tasks.expire_wallet_gifts": {"queue": "default"},
}

NOTE_PROVIDER_MAX_CONCURRENCY = env.int("NOTE_PROVIDER_MAX_CONCURRENCY")
# How many topics of ONE note may be written at once. Always further capped by
# NOTE_PROVIDER_MAX_CONCURRENCY (the global Redis provider gate); 1 restores strictly
# sequential writing.
NOTE_STRUCTURE_PARALLELISM = env.int("NOTE_STRUCTURE_PARALLELISM")
# Topics are written from the planner's key_concepts instead of waiting for the topics they
# refer back to (position-only, so output is reproducible). Off = previous behaviour.
NOTE_TOPIC_PLAN_DIGEST_ENABLED = env.bool("NOTE_TOPIC_PLAN_DIGEST_ENABLED")
# The note completes once its HTML is saved; the PDF renders in its own task. Off = the PDF
# is rendered inside note generation and a PDF failure fails the note.
NOTE_PDF_DEFERRED_ENABLED = env.bool("NOTE_PDF_DEFERRED_ENABLED")
# Free, local OCR (RapidOCR) for the pictures on slide pages that carry little text, so names and labels baked
# into slide images reach the note. It runs in a short-lived child process to keep the worker's memory flat.
SLIDE_OCR_ENABLED = env.bool("SLIDE_OCR_ENABLED")
SLIDE_OCR_MIN_TEXT_CHARS = env.int("SLIDE_OCR_MIN_TEXT_CHARS")
SLIDE_OCR_MIN_IMAGE_PX = env.int("SLIDE_OCR_MIN_IMAGE_PX")
SLIDE_OCR_MIN_CONFIDENCE = env.float("SLIDE_OCR_MIN_CONFIDENCE")
SLIDE_OCR_MAX_IMAGES = env.int("SLIDE_OCR_MAX_IMAGES")
SLIDE_OCR_THREADS = env.int("SLIDE_OCR_THREADS")
SLIDE_OCR_TIMEOUT_SECONDS = env.int("SLIDE_OCR_TIMEOUT_SECONDS")
NOTE_PROVIDER_SLOT_WAIT_SECONDS = env.int("NOTE_PROVIDER_SLOT_WAIT_SECONDS")
NOTE_PROVIDER_SLOT_POLL_SECONDS = env.float("NOTE_PROVIDER_SLOT_POLL_SECONDS")
# Phase 18: Chromium/PDF rendering is CPU/RAM-bound, not provider-I/O-bound,
# so it is deliberately gated by its own, separate, low-default concurrency
# slot (same Redis-backed mechanism as NOTE_PROVIDER_MAX_CONCURRENCY) rather
# than sharing NOTE_PROVIDER_MAX_CONCURRENCY's headroom. This lets
# CELERY_WORKER_NOTES_CONCURRENCY be raised for the LLM-bound portion of
# note generation without multiplying concurrent Chromium subprocesses.
PDF_RENDER_MAX_CONCURRENCY = env.int("PDF_RENDER_MAX_CONCURRENCY")
PDF_RENDER_SLOT_WAIT_SECONDS = env.int("PDF_RENDER_SLOT_WAIT_SECONDS")
PDF_RENDER_SLOT_POLL_SECONDS = env.float("PDF_RENDER_SLOT_POLL_SECONDS")
NOTE_SUMMARY_QUEUE_STALL_SECONDS = env.int("NOTE_SUMMARY_QUEUE_STALL_SECONDS", default=90)
NOTE_SUMMARY_QUEUE_RECOVERY_MAX_ATTEMPTS = env.int(
    "NOTE_SUMMARY_QUEUE_RECOVERY_MAX_ATTEMPTS", default=3
)
NOTE_SUMMARY_RECOVERY_BATCH_SIZE = env.int("NOTE_SUMMARY_RECOVERY_BATCH_SIZE", default=25)
AVANEGAR_PROVIDER_MAX_CONCURRENCY = env.int("AVANEGAR_PROVIDER_MAX_CONCURRENCY")
AVANEGAR_PROVIDER_SLOT_WAIT_SECONDS = env.int("AVANEGAR_PROVIDER_SLOT_WAIT_SECONDS")
AVANEGAR_PROVIDER_SLOT_POLL_SECONDS = env.float("AVANEGAR_PROVIDER_SLOT_POLL_SECONDS")
LECTURE_TASK_LOCK_TTL_SECONDS = env.int("LECTURE_TASK_LOCK_TTL_SECONDS")
LECTURE_PROCESSING_TIMEOUT_SECONDS = env.int("LECTURE_PROCESSING_TIMEOUT_SECONDS")
TRANSCRIPTION_CHUNK_INACTIVITY_TIMEOUT_SECONDS = env.int(
    "TRANSCRIPTION_CHUNK_INACTIVITY_TIMEOUT_SECONDS"
)
NOTE_GENERATION_INACTIVITY_TIMEOUT_SECONDS = env.int("NOTE_GENERATION_INACTIVITY_TIMEOUT_SECONDS")

LOG_LEVEL = env("LOG_LEVEL")
SCALING_BASELINE_METRICS_ENABLED = env.bool("SCALING_BASELINE_METRICS_ENABLED")
SCALING_BASELINE_METRICS_SAMPLE_RATE = min(
    1.0,
    max(0.0, env.float("SCALING_BASELINE_METRICS_SAMPLE_RATE")),
)
AI_COST_TRACKING_ENABLED = env.bool("AI_COST_TRACKING_ENABLED", default=False)
AI_COST_RECONCILIATION_ENABLED = env.bool("AI_COST_RECONCILIATION_ENABLED", default=False)
AI_COST_RECONCILIATION_INITIAL_DELAY_SECONDS = env.int(
    "AI_COST_RECONCILIATION_INITIAL_DELAY_SECONDS", default=60
)
AI_COST_RECONCILIATION_BATCH_SIZE = min(
    1000, max(1, env.int("AI_COST_RECONCILIATION_BATCH_SIZE", default=100))
)
AI_COST_RECONCILIATION_FORCE_AFTER_HOURS = max(
    1, env.int("AI_COST_RECONCILIATION_FORCE_AFTER_HOURS", default=24)
)
AI_COST_RECONCILIATION_HARD_MAX_AGE_DAYS = max(
    1, env.int("AI_COST_RECONCILIATION_HARD_MAX_AGE_DAYS", default=7)
)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": JsonFormatter,
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
        },
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.request": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "celery": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "tesanote": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "tesanote.request": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}
