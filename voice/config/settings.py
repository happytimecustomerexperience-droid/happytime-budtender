"""Django settings for the Happy Time Voice agent.

Lean, single-file, env-driven (Karpathy). Reads a local .env in dev; in Docker the
env is injected by compose. Ported from swedish-bot/config/settings.py and EXTENDED
with the voice/Vapi/budtender env reads (03-CONVENTIONS.md §3) and the P0 prod-fail-
closed boot guard (10-P0-CHASSIS-FAQ.md §1.3).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# ── Core ──────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = _env_bool("DJANGO_DEBUG", "0")  # fail-safe: production unless explicitly on
ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,voice.internal,voice")

# Public HTTPS base Vapi tools call back to (server.url builder).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")

# Origins permitted to embed the dashboard widget / hit the API (CORS).
WIDGET_ALLOWED_ORIGINS = _env_list("WIDGET_ALLOWED_ORIGINS", "http://localhost:8000")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # local apps
    "core",
    "voice",
    "kb",
    "crm",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "core.middleware.WidgetCorsMiddleware",
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
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "dashboard.branding.context_processor",  # P5: brand tokens → CSS vars + logo
            ],
        },
    },
]

# ── Database ──────────────────────────────────────────────────────────
# Postgres is the production engine. The fast offline test plane (03-CONVENTIONS.md
# §5 "Unit — SQLite-OK, no network") flips to in-memory SQLite — automatically under
# pytest (the runner is already imported when settings load) or explicitly via
# HHT_TEST_SQLITE=1 — so the unit/contract suite runs without a DB server or live keys.
import sys as _sys  # noqa: E402

_USE_SQLITE = _env_bool("HHT_TEST_SQLITE", "0") or ("pytest" in _sys.modules)
if _USE_SQLITE:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "happytime_voice"),
            "USER": os.environ.get("POSTGRES_USER", "happytime_voice"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "happytime_voice"),
            "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

# ── i18n / l10n ───────────────────────────────────────────────────────
LANGUAGE_CODE = "en"
TIME_ZONE = "America/Los_Angeles"  # WA stores
USE_I18N = True
USE_TZ = True

# ── Static / media ────────────────────────────────────────────────────
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
MEDIA_URL = "uploads/"
MEDIA_ROOT = BASE_DIR / "data" / "uploads"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── Vapi surface (03-CONVENTIONS.md §3.3) ─────────────────────────────
VAPI_PRIVATE_KEY = os.environ.get("VAPI_PRIVATE_KEY", "")
VAPI_WEBHOOK_SECRET = os.environ.get("VAPI_WEBHOOK_SECRET", "")
VAPI_SQUAD_ID = os.environ.get("VAPI_SQUAD_ID", "")
VAPI_PHONE_NUMBER_ID = os.environ.get("VAPI_PHONE_NUMBER_ID", "")
VAPI_PHONE_NUMBER_STORE_MAP = os.environ.get("VAPI_PHONE_NUMBER_STORE_MAP", "")
VAPI_VOICE_ID = os.environ.get("VAPI_VOICE_ID", "a3520a8f-226a-428d-9fcd-b0a4711a6829")
VAPI_ASSISTANT_MODEL = os.environ.get("VAPI_ASSISTANT_MODEL", "gpt-4.1-mini")
# Webhook auth header names (env-driven; the exact Vapi header is config, not code — 23-SPEC §4.1).
VAPI_SIGNATURE_HEADER = os.environ.get("VAPI_SIGNATURE_HEADER", "X-Vapi-Signature")
VAPI_SECRET_HEADER = os.environ.get("VAPI_SECRET_HEADER", "X-Vapi-Secret")

# ── budtender microservice (03-CONVENTIONS.md §3.4) ───────────────────
HHT_BUDTENDER_BASE_URL = os.environ.get("HHT_BUDTENDER_BASE_URL", "")
HHT_BACKEND_TOKEN = os.environ.get("HHT_BACKEND_TOKEN", "")
HHT_BUDTENDER_TIMEOUT = int(os.environ.get("HHT_BUDTENDER_TIMEOUT", "8"))

# ── Transfer / phone routing (03-CONVENTIONS.md §3.5) ─────────────────
HHT_TRANSFER_NUMBER_YAKIMA = os.environ.get("HHT_TRANSFER_NUMBER_YAKIMA", "")
HHT_TRANSFER_NUMBER_MTVERNON = os.environ.get("HHT_TRANSFER_NUMBER_MTVERNON", "")
HHT_TRANSFER_NUMBER_PULLMAN = os.environ.get("HHT_TRANSFER_NUMBER_PULLMAN", "")
HHT_DEFAULT_STORE = os.environ.get("HHT_DEFAULT_STORE", "yakima")

# ── Post-call queue (P5, gated; 15-P5 §3.5 / ADR-021) ─────────────────
# OFF by default → post-call work (summary/email/rollup) runs INLINE exactly as P2 (the durable
# VoiceCall write is always synchronous). Flip HHT_USE_CELERY=1 + run a worker to move it onto Redis.
HHT_USE_CELERY = _env_bool("HHT_USE_CELERY", "0")
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.environ.get(
    "CELERY_RESULT_BACKEND", ""
)  # results unused (ignore_result)
# Eager = run tasks synchronously in-process (no broker). Auto-on under pytest so the suite is
# broker-free; otherwise honors CELERY_TASK_ALWAYS_EAGER (default off in real deploys).
CELERY_TASK_ALWAYS_EAGER = _env_bool("CELERY_TASK_ALWAYS_EAGER", "0") or ("pytest" in _sys.modules)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_WORKER_CONCURRENCY = int(os.environ.get("CELERY_WORKER_CONCURRENCY", "2"))

# ── Instant Vapi sync (P6) ────────────────────────────────────────────
# ON by default → a dashboard save of an assistant auto-publishes to Vapi (PATCH /assistant +
# /squad) right away, so an edit reflects in the live agent instantly (zero-drift hash keeps a
# no-edit re-save cheap). OFF under pytest so the suite never makes a network call — a publish test
# opts in by setting settings.HHT_AUTO_PUBLISH=True with the Vapi HTTP layer mocked.
HHT_AUTO_PUBLISH = _env_bool("HHT_AUTO_PUBLISH", "1") and ("pytest" not in _sys.modules)

# ── Vendor routing (P3 / 13-P3 §10) ───────────────────────────────────
# The spoken callback window the vendor member states on a no-answer leg (Numbers-Guard source).
HHT_VENDOR_CALLBACK_WINDOW = os.environ.get("HHT_VENDOR_CALLBACK_WINDOW", "one business day")
# Optional n8n/CRM secondary sink for vendor callbacks (O-6/O-9). Off when unset.
VENDOR_CALLBACK_WEBHOOK_URL = os.environ.get("VENDOR_CALLBACK_WEBHOOK_URL", "")

# ── Email / staff alerts (03-CONVENTIONS.md §3.7) ─────────────────────
EMAIL_BACKEND = os.environ.get(
    "DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", "1")
LEAD_EMAIL_FROM = os.environ.get("LEAD_EMAIL_FROM", "bot@happytimeweed.com")
STAFF_ALERT_EMAIL = os.environ.get("STAFF_ALERT_EMAIL", "")
STAFF_ALERT_EMAIL_YAKIMA = os.environ.get("STAFF_ALERT_EMAIL_YAKIMA", "")
STAFF_ALERT_EMAIL_MTVERNON = os.environ.get("STAFF_ALERT_EMAIL_MTVERNON", "")
STAFF_ALERT_EMAIL_PULLMAN = os.environ.get("STAFF_ALERT_EMAIL_PULLMAN", "")

# ── Slack (optional secondary sink; 03-CONVENTIONS.md §3.8) ────────────
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")
SLACK_ALERTS_ENABLED = _env_bool("SLACK_ALERTS_ENABLED", "0")

# ── n8n (optional outbound automation sink; P6) — POST each call event here ───
# Set from the credentials editor (or .env). Empty → the n8n sink is skipped.
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "")

# ── Security hardening / PII (03-CONVENTIONS.md §3.9) ─────────────────
# Phone-hash pepper for returning-caller recognition — MUST differ from SECRET_KEY.
PHONE_HASH_PEPPER = os.environ.get("PHONE_HASH_PEPPER", "dev-pepper-change-me")

# Upload + body limits (oversized-body / decompression-bomb guards).
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

# Webhook/tool rate-limit window (s).
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", "300"))

# Embedding-backed semantic FAQ retrieval. On by default at runtime; the test suite
# disables it (autouse fixture) so unit tests stay offline.
SEMANTIC_SEARCH_ENABLED = _env_bool("SEMANTIC_SEARCH_ENABLED", "1")

CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS", "")

# ── Prod-fail-closed boot guard (10-P0 §1.3; extends swedish-bot L153) ─
if not DEBUG:
    from django.core.exceptions import ImproperlyConfigured

    # Never run production with the dev secret.
    if SECRET_KEY == "dev-insecure-change-me":
        raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in production (DEBUG=0).")
    # The phone-hash pepper must differ from the secret key (returning-caller PII).
    if PHONE_HASH_PEPPER == SECRET_KEY:
        raise ImproperlyConfigured(
            "PHONE_HASH_PEPPER must differ from DJANGO_SECRET_KEY in production (DEBUG=0)."
        )
    # The Vapi webhook gate fails closed without these; refuse to boot if absent.
    _missing = [
        name
        for name in ("VAPI_PRIVATE_KEY", "VAPI_WEBHOOK_SECRET", "HHT_BACKEND_TOKEN")
        if not os.environ.get(name)
    ]
    if _missing:
        raise ImproperlyConfigured(
            f"Missing required prod secrets (DEBUG=0): {', '.join(_missing)}."
        )
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SESSION_COOKIE_HTTPONLY = True
    X_FRAME_OPTIONS = "DENY"
    # TLS-dependent flags: on only behind real HTTPS.
    if _env_bool("HTTPS_ENABLED", "0"):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
        SECURE_SSL_REDIRECT = True
        SECURE_HSTS_SECONDS = 31536000
        SECURE_HSTS_INCLUDE_SUBDOMAINS = True
        SECURE_HSTS_PRELOAD = True
        SESSION_COOKIE_SECURE = True
        CSRF_COOKIE_SECURE = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
