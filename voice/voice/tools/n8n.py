"""``notify_n8n`` — the bot-callable n8n trigger tool (P6).

Lets an assistant fire an n8n automation workflow for a follow-up the caller agreed to (text the
menu link, deals signup, log a callback). POSTs a small leak-safe event to ``N8N_WEBHOOK_URL`` and
returns an acknowledgement; n8n owns the downstream action. Bind it to a bot from the dashboard
(add ``notify_n8n`` to the role's tool names) — it is NOT bound by default.

Complements ``crm.sinks.N8nSink`` (which posts EVERY completed call to n8n). This tool is the
mid-call, intent-driven direction. Leak/PII-safe: no product/cost/margin fields; no raw phone (the
caller's number is never put in the payload — only the call id + a spoken summary).
"""

from __future__ import annotations

import json
import logging
import urllib.request

from django.conf import settings

from voice.constants import spoken_store
from voice.tools import register

logger = logging.getLogger(__name__)


@register("notify_n8n")
def notify_n8n(args: dict, ctx: dict) -> dict:
    """Queue an n8n workflow trigger for a caller-requested follow-up. Degrade-safe: returns a
    structured ``{ok: false, reason}`` when n8n is unconfigured or the POST fails — never raises
    (dispatch already wraps handlers, but we keep the spoken envelope clean)."""
    url = getattr(settings, "N8N_WEBHOOK_URL", "") or ""
    event_type = (args.get("event_type") or "").strip()
    if not event_type:
        return {"ok": False, "reason": "missing event_type"}
    if not url:
        return {"ok": False, "reason": "n8n not configured"}

    store = (args.get("store") or ctx.get("store") or "").strip()
    payload = {
        "event": "bot_action",
        "event_type": event_type,
        "summary": (args.get("summary") or "").strip(),
        "store": store,
        "store_spoken": spoken_store(store) if store else "",
        "call_id": ctx.get("call_id", ""),
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (config-supplied URL)
            if r.status >= 300:
                return {"ok": False, "reason": f"n8n HTTP {r.status}"}
    except Exception as exc:  # noqa: BLE001 — a webhook hiccup is a soft failure, not a crash
        logger.warning("notify_n8n POST failed: %s", exc)
        return {"ok": False, "reason": "n8n unreachable"}
    return {"ok": True, "queued": True, "event_type": event_type}
