"""
Django settings for the Happy Time Budtender service.

Security posture: this service only ever answers the website's server-side
proxy (Bearer token), runs behind a Cloudflare tunnel, and never returns
cost/margin to any caller. DEBUG must stay False in production.
"""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")
# Dashboard-sourced Dutchie credentials live in their own file (gitignored) so
# they never collide with placeholders and stay easy to rotate. Overrides .env.
load_dotenv(BASE_DIR / ".env.dutchie", override=True)


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    return env(key, str(default)).lower() in ("1", "true", "yes", "on")


SECRET_KEY = env("SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env_bool("DEBUG", False)
ALLOWED_HOSTS = [h.strip() for h in env("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Service token the website presents. Required in production.
HHT_BACKEND_TOKEN = env("HHT_BACKEND_TOKEN", "")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "rest_framework",
    "django_celery_beat",
    "budtender",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "core.urls"
WSGI_APPLICATION = "core.wsgi.application"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [], "APP_DIRS": True, "OPTIONS": {}}]

DATABASES = {
    "default": {
        "ENGINE": env("SQL_ENGINE", "django.db.backends.postgresql"),
        "NAME": env("SQL_DATABASE", "budtender"),
        "USER": env("SQL_USER", "budtender"),
        "PASSWORD": env("SQL_PASSWORD", ""),
        "HOST": env("SQL_HOST", "localhost"),
        "PORT": env("SQL_PORT", "5432"),
        # Reuse connections across requests so bursts don't pay connect cost.
        "CONN_MAX_AGE": int(env("SQL_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
USE_TZ = True
TIME_ZONE = "America/Los_Angeles"

# ── DRF: token-only, no browsable API ────────────────────────────────────────
# NO IP throttle: this API is called ONLY by the website's server (one shared
# IP, Bearer-gated). An IP throttle would throttle ALL end users collectively.
# The ServiceTokenPermission Bearer gate is the security boundary.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["budtender.auth.ServiceTokenPermission"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
}

# ── Celery ───────────────────────────────────────────────────────────────────
CELERY_BROKER_URL = env("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_BACKEND_URL", "redis://localhost:6379/0")
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_EAGER", False)

REDIS_URL = env("REDIS_URL", "redis://localhost:6379/1")
CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}
}

# ── Dutchie per-store config (copied from marketing_dashboard) ────────────────
def _users() -> list:
    try:
        return json.loads(env("DUTCHIE_BACKOFFICE_USERS", "[]"))
    except json.JSONDecodeError:
        return []


DUTCHIE = {
    "backoffice_base_url": env("DUTCHIE_BACKOFFICE_BASE_URL", "https://ash.backoffice.dutchie.com/"),
    "backoffice_users": _users(),
    "stores": {
        "yakima": {
            "pos_key": env("DUTCHIE_YAKIMA_POS_KEY"),
            "loc_id": env("DUTCHIE_YAKIMA_LOC_ID"),
            "lsp_id": env("DUTCHIE_YAKIMA_LSP_ID"),
        },
        "mount-vernon": {
            "pos_key": env("DUTCHIE_MTVERNON_POS_KEY"),
            "loc_id": env("DUTCHIE_MTVERNON_LOC_ID"),
            "lsp_id": env("DUTCHIE_MTVERNON_LSP_ID"),
        },
        "pullman": {
            "pos_key": env("DUTCHIE_PULLMAN_POS_KEY"),
            "loc_id": env("DUTCHIE_PULLMAN_LOC_ID"),
            "lsp_id": env("DUTCHIE_PULLMAN_LSP_ID"),
        },
    },
}

# ── Production hardening (only when not DEBUG) ────────────────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = False  # TLS terminates at Cloudflare tunnel
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
