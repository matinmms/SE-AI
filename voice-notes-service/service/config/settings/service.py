"""Settings for the standalone voice -> note (v2.2) service.

Everything not listed here keeps the value from ``base.py`` (models, providers, prompts, limits),
which is exactly what TesaNote runs. Only the pieces that change when the pipeline runs on its own
are set below. The one secret you must provide is AVALAI_API_KEY (see ../.env.example).
"""
from pathlib import Path

import environ as _environ

# The single .env lives at the root of the service folder (one level above this Django project).
_environ.Env.read_env(Path(__file__).resolve().parents[3] / ".env")

from .base import *  # noqa: E402,F403

# --- apps: only what the pipeline needs (their models are referenced by the pipeline) -------------
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django_celery_beat",
    "apps.accounts",
    "apps.common",
    "apps.lectures",
    "apps.uploads",
    "apps.notes",
    "apps.referrals",
    "apps.ai_pipeline",
    "apps.audio_pipeline",
    "apps.subscriptions",
    "apps.usage",
    "apps.payments",
    "apps.wallet",
    "apps.admin_ops",
    "apps.ai_costs",
    "apps.storage_lifecycle",
]
MIDDLEWARE: list[str] = []
ROOT_URLCONF = "config.urls"
DEBUG = False

# --- database: VNS_DATABASE_URL (Postgres in production). SQLite is fine for one-off local runs ---
_data_dir = Path(env("VNS_DATA_DIR", default=str(BASE_DIR / "data")))  # noqa: F405
_database_url = env("VNS_DATABASE_URL", default="")  # noqa: F405
if _database_url:
    DATABASES = {"default": env.db_url_config(_database_url)}  # noqa: F405
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": _data_dir / "service.sqlite3"}}
DATABASES["default"]["CONN_MAX_AGE"] = 0

# --- files: local disk. Each job's audio and chunks live under VNS_DATA_DIR/media ----------------
STORAGE_MODE = "local"
MEDIA_ROOT = _data_dir / "media"
STATIC_ROOT = _data_dir / "static"

# --- queue: a job runs start-to-finish inside the calling process (no Celery worker needed) ------
HEALTH_CHECK_REDIS_ENABLED = False
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# --- what this service produces: the "lecturer's words" note (v2.2) ------------------------------
NOTE_STYLE_CHOICE_ENABLED = True
NOTE_STYLE_CHOICE_STAFF_ONLY = False
NOTE_QUALITY_V21_ENABLED = True
NOTE_QUALITY_V42_ENABLED = True  # revision 3 = v2.2 for the lecturer style

# --- no billing here: jobs are never charged or limited by minutes -------------------------------
WALLET_BILLING_ENABLED = False
WALLET_BILLING_FOR_ALL = False
WALLET_BILLING_PHONE_NUMBERS: list[str] = []
