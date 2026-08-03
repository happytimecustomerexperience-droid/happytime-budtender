"""
Django settings for the Happy Time Budtender service.

Security posture: this service only ever answers the website's server-side
proxy (Bearer token), runs behind a Cloudflare tunnel, and never returns
cost/margin to any caller. DEBUG must stay False in production.
"""
import json
import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
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
# Always allow the in-cluster service aliases (internal-only): the voice service reaches budtender at
# http://budtender.internal:8000 and http://web:8000, so a stale/short ALLOWED_HOSTS in a deploy's .env
# must never DisallowedHost-400 those calls. These are docker-network names, not public hostnames.
for _alias in ("localhost", "127.0.0.1", "budtender.internal", "web"):
    if _alias not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_alias)
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Service token the website presents. Required in production.
HHT_BACKEND_TOKEN = env("HHT_BACKEND_TOKEN", "")

# ── Bundle landing (/custom-order) ───────────────────────────────────────────
# Shared with alpine-automations, which SIGNS the emailed links this app VERIFIES.
# The URL is effectively a coupon (it tells a budtender "this person gets 30% off"),
# so an unset secret must fail closed rather than accept anything — see
# _prod_guard_errors and bundles/signing.py.
BUNDLE_URL_SECRET = env("BUNDLE_URL_SECRET", "")
# "if >1 is in stock we can propose it" — anything at exactly 1 is one walk-in
# away from being gone before the shopper arrives.
BUNDLE_MIN_STOCK = int(env("BUNDLE_MIN_STOCK", "2"))
BUNDLE_DRAFT_TTL_HOURS = int(env("BUNDLE_DRAFT_TTL_HOURS", "4"))
# FLOOR for the online-order cap, not the cap itself. Nothing is paid up front, so
# an unbounded order is unbounded staff labour and real held inventory against no
# commitment — but the live number is calibrated weekly from real basket totals
# (bundles/calibration.py) and stored per store. This value applies until a store
# has enough sales to calibrate, and stops a quiet quarter calibrating it downward.
BUNDLE_MAX_ORDER_TOTAL = float(env("BUNDLE_MAX_ORDER_TOTAL", "300"))

# ── Email (order confirmations) ──────────────────────────────────────────────
# Unset EMAIL_HOST => locmem/dummy backend and nothing is sent; bundles.emails
# checks `enabled()` first, so an unconfigured mail server is a silent no-op
# rather than a failed checkout.
EMAIL_HOST = env("EMAIL_HOST", "")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_TIMEOUT = int(env("EMAIL_TIMEOUT", "10"))
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "")
EMAIL_BACKEND = (
    "django.core.mail.backends.smtp.EmailBackend" if EMAIL_HOST
    else "django.core.mail.backends.dummy.EmailBackend"
)
HHT_VOICE_BASE_URL = env("HHT_VOICE_BASE_URL", "")
HHT_VOICE_TIMEOUT = int(env("HHT_VOICE_TIMEOUT", "5"))


def _prod_guard_errors(secret_key: str, backend_token: str,
                       bundle_secret: str = "unchecked") -> list[str]:
    errors = []
    if secret_key == "insecure-dev-key-change-me":
        errors.append("SECRET_KEY")
    if not backend_token.strip():
        errors.append("HHT_BACKEND_TOKEN")
    # An unset bundle secret makes every /custom-order link fail verification, so
    # the page would 400 for every recipient of a live campaign. Fail at boot
    # instead of at open rate. Default keeps existing callers passing.
    if bundle_secret != "unchecked" and not bundle_secret.strip():
        errors.append("BUNDLE_URL_SECRET")
    return errors

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",       # POS staff login sessions
    "django.contrib.staticfiles",
    "rest_framework",
    "django_celery_beat",
    "budtender",
    # ── merged-in POS (in-store register screens, own tables) ──
    "pos",
    "customers",
    # ── public /custom-order bundle landing (no models, no migrations) ──
    "bundles",
]

# The DRF API stays token-only (no session auth → CSRF inert for /api/v1). The POS
# HTML screens add sessions/auth/CSRF/CSP/whitenoise on top; both coexist in one app.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",              # serve POS static at scale
    "pos_core.security.ContentSecurityPolicyMiddleware",       # CSP header for the POS HTML
    "django.contrib.sessions.middleware.SessionMiddleware",    # POS login
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",               # guards POS HTML forms only
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"
WSGI_APPLICATION = "core.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,   # finds pos/templates/pos/*
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
    ]},
}]

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
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}
LOGIN_URL = "login"  # POS staff screens are @login_required
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

# Cache: Redis when configured (prod/docker), else in-process LocMem so dev and the
# POS menu cache work without a running Redis. Under pytest we always use LocMem so a
# deploy .env's REDIS_URL can't drag the suite onto an unreachable Redis (mirrors the
# prod-guard's pytest check above). Matches the POS's original graceful default.
REDIS_URL = env("REDIS_URL", "")
if REDIS_URL and "pytest" not in sys.modules:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

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
    # ponytail: pytest imports settings before per-test env overrides; real DEBUG=0 processes fail closed.
    if "pytest" not in sys.modules:
        if _missing := _prod_guard_errors(SECRET_KEY, HHT_BACKEND_TOKEN, BUNDLE_URL_SECRET):
            raise ImproperlyConfigured(
                f"Missing required prod settings (DEBUG=0): {', '.join(_missing)}."
            )
    SECURE_SSL_REDIRECT = False  # TLS terminates at Cloudflare tunnel
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    # POS HTML surface hardening (the token API sets no session cookie).
    SESSION_COOKIE_HTTPONLY = True
    CSRF_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SAMESITE = "Lax"
    X_FRAME_OPTIONS = "DENY"
