"""
Django settings for the Playto Payout Engine.

Loaded from environment variables (see .env.example). PostgreSQL is mandatory;
SQLite is intentionally rejected because the engine relies on row-level locks
(SELECT FOR UPDATE) and integer-only money arithmetic that we want to keep
honest in tests too.
"""
from __future__ import annotations

import os
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() == "true"
ALLOWED_HOSTS = [
    h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",") if h.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "django_celery_beat",
    "payouts",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

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

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is required. Use the Supabase Session Pooler connection "
        "(host: aws-0-<region>.pooler.supabase.com, port: 5432, user: "
        "postgres.<project-ref>). The Transaction Pooler (port 6543) does not "
        "preserve session state and will silently break SELECT FOR UPDATE. "
        "The legacy direct connection (db.<ref>.supabase.co) is IPv6-only on "
        "free tier and fails DNS on most macOS / consumer networks."
    )

DATABASES = {
    "default": dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=0,  # short-lived connections; safer behind a pooler
        ssl_require=not DEBUG or "supabase.co" in DATABASE_URL,
    ),
}
# Test database name — overridable so multiple developers can run tests
# against one Supabase instance without colliding.
DATABASES["default"]["TEST"] = {
    "NAME": os.environ.get("DJANGO_TEST_DB_NAME", "test_playto"),
}
# Tests use a local SQLite DB only when explicitly opted in; the real test
# suite for concurrency must run against PostgreSQL.
if os.environ.get("USE_SQLITE_FOR_TESTS") == "1":
    DATABASES["default"] = {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ("rest_framework.renderers.JSONRenderer",),
    "DEFAULT_PARSER_CLASSES": ("rest_framework.parsers.JSONParser",),
    "EXCEPTION_HANDLER": "payouts.exceptions.payout_exception_handler",
}

CORS_ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ALLOWED_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]
CORS_ALLOW_HEADERS = [
    "accept",
    "accept-encoding",
    "authorization",
    "content-type",
    "dnt",
    "origin",
    "user-agent",
    "x-csrftoken",
    "x-requested-with",
    "idempotency-key",
    "x-merchant-id",
]

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Payout knobs
PAYOUT_SUCCESS_RATE = float(os.environ.get("PAYOUT_SUCCESS_RATE", "0.7"))
PAYOUT_FAILURE_RATE = float(os.environ.get("PAYOUT_FAILURE_RATE", "0.2"))
PAYOUT_STUCK_TIMEOUT_SECONDS = int(os.environ.get("PAYOUT_STUCK_TIMEOUT_SECONDS", "30"))
PAYOUT_MAX_RETRIES = int(os.environ.get("PAYOUT_MAX_RETRIES", "3"))
IDEMPOTENCY_TTL_HOURS = int(os.environ.get("IDEMPOTENCY_TTL_HOURS", "24"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "payouts": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
