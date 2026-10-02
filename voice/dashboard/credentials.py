"""Dashboard credentials — the editable catalog + apply-to-runtime helpers (P6).

The owner edits secrets/config from the dashboard; ``set_credential`` persists the value AND makes
it live by writing both ``os.environ[name]`` and ``settings.<name>`` (Django settings are a live
module object, so ``getattr(settings, name)`` readers see the new value immediately).

A save must reach EVERY process, not only the one that handled it (the web workers and the Celery
worker that sends the alerts are separate processes). Each save/clear writes a fresh version token
into Django's cache (Redis in prod, shared by all containers — ``CACHES`` in settings);
``refresh_if_stale`` — called at the start of each web request and each Celery task — re-applies
every stored row when the token differs from the one this process last applied. No usable cache →
the token is always absent → each process applies once, as before.

Provider keys for ElevenLabs / Google (Gemini) are NOT here — Vapi resolves those from ITS own
dashboard (Settings → Integrations); there is no public Vapi credential API (verified against the
live OpenAPI spec). This page manages OUR secrets: the Vapi API key + webhook secret, the budtender
token + URL, the per-store transfer numbers, SMTP, Slack, and the n8n webhook URL.

Secrets are write-only: the page shows set / not set, never a character of the value. URL entries
carry an ``allow`` rule, checked on save, so a saved value can never point the service's outbound
calls (and the Bearer token they carry) at an arbitrary host.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import threading
import uuid
from urllib.parse import urlparse

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

# group, name (ENV/settings var), label, secret?, help, optional allow-rule key. Order = display order.
CREDENTIAL_CATALOG: list[dict] = [
    {"group": "Vapi", "name": "VAPI_PRIVATE_KEY", "label": "Vapi private key", "secret": True,
     "help": "Bearer key for the Vapi REST API (provision, publish, call fetch). Live immediately."},
    {"group": "Vapi", "name": "VAPI_PUBLIC_KEY", "label": "Vapi public key", "secret": False,
     "help": "The PUBLIC key the browser test console uses for its real-call mode."},
    {"group": "Vapi", "name": "VAPI_WEBHOOK_SECRET", "label": "Vapi webhook secret", "secret": True,
     "help": "Shared secret the inbound webhook verifies (fail-closed)."},
    {"group": "Vapi", "name": "VAPI_SQUAD_ID", "label": "Vapi squad id", "secret": False,
     "help": "Provisioned Squad id (publish target)."},
    {"group": "Vapi", "name": "VAPI_PHONE_NUMBER_ID", "label": "Vapi phone number id", "secret": False,
     "help": "Inbound number fronting the Squad."},
    {"group": "Budtender", "name": "HHT_BUDTENDER_BASE_URL", "label": "Budtender base URL", "secret": False,
     "allow": "budtender_url",
     "help": "Base URL of the happytime-budtender service. Only http://web:8000 or an https:// "
             "address on a happytimeweed.com subdomain is accepted."},
    {"group": "Budtender", "name": "HHT_BACKEND_TOKEN", "label": "Budtender service token", "secret": True,
     "help": "Bearer token shared with budtender (must match its side)."},
    {"group": "Transfer numbers", "name": "HHT_TRANSFER_NUMBER_YAKIMA", "label": "Yakima transfer #", "secret": False,
     "help": "E.164 warm-transfer destination for Yakima."},
    {"group": "Transfer numbers", "name": "HHT_TRANSFER_NUMBER_MTVERNON", "label": "Mt Vernon transfer #", "secret": False,
     "help": "E.164 warm-transfer destination for Mount Vernon."},
    {"group": "Transfer numbers", "name": "HHT_TRANSFER_NUMBER_PULLMAN", "label": "Pullman transfer #", "secret": False,
     "help": "E.164 warm-transfer destination for Pullman."},
    {"group": "Transfer heads-up", "name": "PUSHOVER_APP_TOKEN", "label": "Pushover app token", "secret": True,
     "help": "Application token from pushover.net. With a store's user key below, staff phones get a push "
             "describing who is calling when a call is transferred."},
    {"group": "Transfer heads-up", "name": "PUSHOVER_USER_YAKIMA", "label": "Yakima Pushover user key", "secret": True,
     "help": "Pushover user (or group) key whose phones are pushed for Yakima transfers."},
    {"group": "Transfer heads-up", "name": "PUSHOVER_USER_MTVERNON", "label": "Mt Vernon Pushover user key", "secret": True,
     "help": "Pushover user (or group) key for Mount Vernon transfers."},
    {"group": "Transfer heads-up", "name": "PUSHOVER_USER_PULLMAN", "label": "Pullman Pushover user key", "secret": True,
     "help": "Pushover user (or group) key for Pullman transfers."},
    {"group": "Transfer heads-up", "name": "HHT_TRANSFER_NOTICE_DAILY_CAP", "label": "Transfer notices per day", "secret": False,
     "help": "Most transfer heads-ups sent in any 24 hours, all stores together (default 50). A runaway "
             "loop stops here."},
    {"group": "Email", "name": "STAFF_ALERT_EMAIL", "label": "Staff alert email", "secret": False,
     "help": "Where per-call summaries + alerts are sent."},
    {"group": "Email", "name": "EMAIL_HOST_PASSWORD", "label": "SMTP password", "secret": True,
     "help": "SMTP/Resend API key used to send staff alerts."},
    {"group": "Integrations", "name": "N8N_WEBHOOK_URL", "label": "n8n webhook URL", "secret": False,
     "allow": "n8n_url",
     "help": "Default n8n workflow webhook the bot can call as a tool (see n8n config). Must be an "
             "https:// address on a public host name (no IP address, localhost or internal name)."},
    {"group": "Integrations", "name": "SLACK_WEBHOOK_URL", "label": "Slack webhook URL", "secret": True,
     "allow": "slack_url",
     "help": "Optional Slack incoming-webhook for urgent alerts. Must start with https://hooks.slack.com/."},
]

_CATALOG_BY_NAME = {c["name"]: c for c in CREDENTIAL_CATALOG}


def is_known(name: str) -> bool:
    return name in _CATALOG_BY_NAME


def current_value(name: str) -> str:
    """The value the app would use right now (DB override already applied to env on startup)."""
    return os.environ.get(name, "") or str(getattr(settings, name, "") or "")


# ── URL allowlists (checked on save) ──────────────────────────────────────────
def _https(url: str):
    """``urlparse`` result for a plain https URL with a host and no userinfo; else None.
    Whitespace, control characters and backslashes are refused outright — they are how a URL gets
    read as one host by this check and another by the HTTP client."""
    if re.search(r"[\s\\\x00-\x1f\x7f]", url):
        return None
    try:
        p = urlparse(url)
        _ = p.port  # raises on a malformed port
    except ValueError:
        return None
    if p.scheme != "https" or not p.hostname or p.username is not None or p.password is not None:
        return None
    return p


def _ok_budtender(url: str) -> bool:
    if url == "http://web:8000":
        return True
    p = _https(url)
    return bool(p and p.hostname.endswith(".happytimeweed.com"))


# A public DNS name only: last label alphabetic (so 127.1, 2130706433 and 0x7f.0.0.1 — which a
# resolver reads as IPv4 — are out), at least one dot, and no internal-only suffix.
_PUBLIC_TLD = re.compile(r"[a-z]{2,}|xn--[a-z0-9-]+")
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa")


def _ok_n8n(url: str) -> bool:
    p = _https(url)
    if not p:
        return False
    host = p.hostname  # lower-cased by urlparse; IPv6 brackets already removed
    try:
        ipaddress.ip_address(host)
        return False  # any IP literal: loopback, private, link-local (169.254.x) or public
    except ValueError:
        pass
    labels = host.split(".")
    return (
        len(labels) >= 2
        and bool(_PUBLIC_TLD.fullmatch(labels[-1]))
        and not host.endswith(_INTERNAL_SUFFIXES)
    )


def _ok_slack(url: str) -> bool:
    p = _https(url)
    return bool(p and p.hostname == "hooks.slack.com" and url.startswith("https://hooks.slack.com/"))


# allow-rule key -> (predicate, staff-facing error)
_ALLOW = {
    "budtender_url": (
        _ok_budtender,
        "Not saved: must be http://web:8000 or an https:// address on a happytimeweed.com subdomain.",
    ),
    "n8n_url": (
        _ok_n8n,
        "Not saved: must be an https:// address on a public host name (no IP address, localhost "
        "or internal name).",
    ),
    "slack_url": (_ok_slack, "Not saved: must start with https://hooks.slack.com/."),
}


def validate(name: str, value: str) -> str | None:
    """The inline error for a value this entry's ``allow`` rule rejects, or None when it is fine."""
    rule = _ALLOW.get(_CATALOG_BY_NAME[name].get("allow", ""))
    if rule is None or rule[0](value):
        return None
    return rule[1]


# ── Apply to this process + tell the others ───────────────────────────────────
_MISSING = object()
_NEVER = object()
_VERSION_KEY = "dashboard:credentials:version"
_lock = threading.RLock()
# name -> (env value | None, settings value | _MISSING) as they were BEFORE the first stored
# override was applied in this process: what a clear falls back to.
_BASELINE: dict[str, tuple[str | None, object]] = {}
_applied: object = _NEVER  # the cache version this process last applied (None = cache had none)


def _apply_one(name: str, value: str) -> None:
    with _lock:
        if name not in _BASELINE:
            _BASELINE[name] = (os.environ.get(name), getattr(settings, name, _MISSING))
        os.environ[name] = value
        setattr(settings, name, value)


def _restore(name: str) -> None:
    """Put back the env/.env value this process had before it applied a stored override."""
    with _lock:
        if name not in _BASELINE:
            return  # never overridden here: the live value already IS the env/.env one
        env, conf = _BASELINE.pop(name)
        if env is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = env
        if conf is _MISSING:
            try:
                delattr(settings, name)
            except AttributeError:
                pass
        else:
            setattr(settings, name, conf)


def _bump_version() -> None:
    """Publish a fresh token so every other process re-applies; this one already has."""
    global _applied
    token = uuid.uuid4().hex
    try:
        cache.set(_VERSION_KEY, token, None)
    except Exception:  # noqa: BLE001 — cache down: other processes keep their last-applied values
        logger.warning("credentials: could not publish the version token to the cache", exc_info=True)
        return
    _applied = token


def set_credential(name: str, value: str) -> None:
    """Persist + apply live: write the Credential row and update os.environ + settings so every
    reader (os.environ-based or settings-based) sees the new value without a restart — here at
    once, in the other processes at the start of their next request / task."""
    from .models import Credential

    Credential.objects.update_or_create(name=name, defaults={"value": value})
    apply_all()  # the whole stored set, not just this name: this process may not have applied the rest
    _bump_version()


def clear_credential(name: str) -> bool:
    """Delete the stored row and fall back to the env/.env value. True when a row existed."""
    from .models import Credential

    deleted, _ = Credential.objects.filter(name=name).delete()
    apply_all()  # the row is gone → its name falls back to the env/.env value
    _bump_version()
    return bool(deleted)


def apply_all() -> int | None:
    """Make this process match the stored rows: apply each over the .env default, and put back the
    .env value of any name whose row is gone (a clear done elsewhere). Returns the count applied,
    or None when the DB is not ready (first migrate) so the caller tries again later."""
    try:
        from .models import Credential

        rows = list(Credential.objects.all())
    except Exception:  # noqa: BLE001 — DB not ready → nothing to apply yet, never crash boot
        return None
    stored = {c.name: c.value for c in rows if c.value}
    for name, value in stored.items():
        _apply_one(name, value)
    for name in [n for n in _BASELINE if n not in stored]:
        _restore(name)
    return len(rows)


def refresh_if_stale() -> None:
    """Re-apply the stored credentials when the shared version token differs from the one this
    process last applied. One ``cache.get`` on the hot path. Never raises."""
    global _applied
    try:
        version = cache.get(_VERSION_KEY)
    except Exception:  # noqa: BLE001 — no usable cache: apply once, as before
        version = None
    if _applied is not _NEVER and version == _applied:
        return
    if apply_all() is not None:
        _applied = version


def catalog_item(entry: dict, stored: set[str]) -> dict:
    """One catalog entry for the template. ``preview`` is the value for non-secrets only — a secret
    is write-only: the page says set / not set and shows no character of it."""
    val = current_value(entry["name"])
    return {
        **entry,
        "is_set": bool(val),
        "stored": entry["name"] in stored,
        "preview": "" if entry["secret"] else val,
    }


def stored_names() -> set[str]:
    from .models import Credential

    return set(Credential.objects.exclude(value="").values_list("name", flat=True))


def catalog_with_values() -> list[dict]:
    """The catalog grouped for the template."""
    stored = stored_names()
    groups: dict[str, list[dict]] = {}
    for c in CREDENTIAL_CATALOG:
        groups.setdefault(c["group"], []).append(catalog_item(c, stored))
    return [{"group": g, "items": items} for g, items in groups.items()]
