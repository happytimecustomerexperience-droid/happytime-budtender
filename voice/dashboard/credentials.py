"""Dashboard credentials — the editable catalog + apply-to-runtime helpers (P6).

The owner edits secrets/config from the dashboard; ``set_credential`` persists the value AND makes
it live by writing both ``os.environ[name]`` and ``settings.<name>`` (Django settings are a live
module object, so ``getattr(settings, name)`` readers see the new value immediately). On startup
``DashboardConfig.ready`` calls ``apply_all`` so DB overrides re-assert over the .env defaults.

Provider keys for ElevenLabs / Google (Gemini) are NOT here — Vapi resolves those from ITS own
dashboard (Settings → Integrations); there is no public Vapi credential API (verified against the
live OpenAPI spec). This page manages OUR secrets: the Vapi API key + webhook secret, the budtender
token + URL, the per-store transfer numbers, SMTP, Slack, and the n8n webhook URL.
"""

from __future__ import annotations

import logging
import os

from django.conf import settings

logger = logging.getLogger(__name__)

# group, name (ENV/settings var), label, secret?, help. Order = display order.
CREDENTIAL_CATALOG: list[dict] = [
    {"group": "Vapi", "name": "VAPI_PRIVATE_KEY", "label": "Vapi private key", "secret": True,
     "help": "Bearer key for the Vapi REST API (provision, publish, call fetch). Live immediately."},
    {"group": "Vapi", "name": "VAPI_WEBHOOK_SECRET", "label": "Vapi webhook secret", "secret": True,
     "help": "Shared secret the inbound webhook verifies (fail-closed)."},
    {"group": "Vapi", "name": "VAPI_SQUAD_ID", "label": "Vapi squad id", "secret": False,
     "help": "Provisioned Squad id (publish target)."},
    {"group": "Vapi", "name": "VAPI_PHONE_NUMBER_ID", "label": "Vapi phone number id", "secret": False,
     "help": "Inbound number fronting the Squad."},
    {"group": "Budtender", "name": "HHT_BUDTENDER_BASE_URL", "label": "Budtender base URL", "secret": False,
     "help": "Base URL of the happytime-budtender service."},
    {"group": "Budtender", "name": "HHT_BACKEND_TOKEN", "label": "Budtender service token", "secret": True,
     "help": "Bearer token shared with budtender (must match its side)."},
    {"group": "Transfer numbers", "name": "HHT_TRANSFER_NUMBER_YAKIMA", "label": "Yakima transfer #", "secret": False,
     "help": "E.164 warm-transfer destination for Yakima."},
    {"group": "Transfer numbers", "name": "HHT_TRANSFER_NUMBER_MTVERNON", "label": "Mt Vernon transfer #", "secret": False,
     "help": "E.164 warm-transfer destination for Mount Vernon."},
    {"group": "Transfer numbers", "name": "HHT_TRANSFER_NUMBER_PULLMAN", "label": "Pullman transfer #", "secret": False,
     "help": "E.164 warm-transfer destination for Pullman."},
    {"group": "Email", "name": "STAFF_ALERT_EMAIL", "label": "Staff alert email", "secret": False,
     "help": "Where per-call summaries + alerts are sent."},
    {"group": "Email", "name": "EMAIL_HOST_PASSWORD", "label": "SMTP password", "secret": True,
     "help": "SMTP/Resend API key used to send staff alerts."},
    {"group": "Integrations", "name": "N8N_WEBHOOK_URL", "label": "n8n webhook URL", "secret": False,
     "help": "Default n8n workflow webhook the bot can call as a tool (see n8n config)."},
    {"group": "Integrations", "name": "SLACK_WEBHOOK_URL", "label": "Slack webhook URL", "secret": True,
     "help": "Optional Slack incoming-webhook for urgent alerts."},
]

_CATALOG_BY_NAME = {c["name"]: c for c in CREDENTIAL_CATALOG}


def is_known(name: str) -> bool:
    return name in _CATALOG_BY_NAME


def current_value(name: str) -> str:
    """The value the app would use right now (DB override already applied to env on startup)."""
    return os.environ.get(name, "") or str(getattr(settings, name, "") or "")


def mask(value: str) -> str:
    """A safe preview of a secret — never the full value."""
    if not value:
        return ""
    if len(value) <= 6:
        return "••••"
    return f"{value[:3]}…{value[-2:]}"


def set_credential(name: str, value: str) -> None:
    """Persist + apply live: write the Credential row and update os.environ + settings so every
    reader (os.environ-based or settings-based) sees the new value without a restart."""
    from .models import Credential

    Credential.objects.update_or_create(name=name, defaults={"value": value})
    _apply_one(name, value)


def _apply_one(name: str, value: str) -> None:
    os.environ[name] = value
    setattr(settings, name, value)


def apply_all() -> int:
    """Re-assert every stored Credential over the .env defaults (called from app startup). Returns
    the count applied. Swallows DB-not-ready errors so a fresh/migrating DB never crashes boot."""
    try:
        from .models import Credential

        rows = list(Credential.objects.all())
    except Exception:  # noqa: BLE001 — DB not ready (first migrate) → nothing to apply yet
        return 0
    for c in rows:
        if c.value:
            _apply_one(c.name, c.value)
    return len(rows)


def catalog_with_values() -> list[dict]:
    """The catalog grouped for the template: each entry + its masked current value + set flag."""
    groups: dict[str, list[dict]] = {}
    for c in CREDENTIAL_CATALOG:
        val = current_value(c["name"])
        groups.setdefault(c["group"], []).append(
            {**c, "is_set": bool(val), "preview": mask(val) if c["secret"] else val}
        )
    return [{"group": g, "items": items} for g, items in groups.items()]
