"""Agent test console — text or talk to the live brain and watch every tool call.

Three ways in, one audit trail out:

* **type** / **browser mic** → this module calls ``voice.chat.answer_text_chat`` in-process.
  That is the SAME brain the Vapi phone agent and the website chat both run, so the answer,
  the grounding decision, the tool picks and the escalation flag are the real ones. The mic
  path is transcribed client-side by the browser's Web Speech API and POSTs identical text —
  it exercises our agent, NOT Vapi's speech layer.
* **real Vapi call** → the page opens a live web call with Vapi's browser SDK. That one goes
  through Vapi's own ASR/LLM/TTS and hits the signed webhook, so it is logged by
  ``voice.webhooks`` exactly like a phone call. Nothing here intercepts it.

Every console turn is persisted through the EXISTING call models (``VoiceCall`` /
``VoiceTurn`` / ``VoiceToolCall``) under a ``pg-<uuid>`` call id with ``source="playground"``,
so a test session is readable from the normal dashboard call log / history / transcript pages.
No second storage layer, no second audit trail.

Boundaries: staff-gated and never customer-facing; it does NOT bypass the signed Vapi webhook
(that path stays fail-closed) and it does NOT weaken the leak-guard — results are scrubbed by
``voice.tools.dispatch`` before they get here and PII-masked again on the way to the DB.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

STORES = ("yakima", "mount-vernon", "pullman")

# The scripted pathways the console offers as one-click buttons. Each is a real caller
# utterance that routes to a DIFFERENT branch of voice.chat, so clicking down the list
# walks the whole agent surface. Kept here (not in the template) so the pathway list is
# testable and lives next to the code that routes it.
SCRIPTED_PROBES = (
    ("Hours (grounded FAQ)", "what are your hours today"),
    ("Return policy (sources required)", "what is your return policy on a broken cart"),
    ("Specials", "what specials do you have"),
    ("Flower by effect", "I want some relaxing indica flower"),
    ("Edible by dose", "do you have 10mg gummies"),
    ("Cartridge under budget", "a cartridge for focus under $40"),
    ("Concentrate, DOH only", "medically compliant concentrate"),
    ("Pre-roll", "cheapest pre-roll you have"),
    ("Escalation / angry caller", "this cart is broken and I want to talk to a human"),
    ("Numbers-guard probe", "how many milligrams of THC is in the blue dream exactly"),
    ("Vendor call", "hi this is a sales rep calling about wholesale pricing"),
)


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_store(value) -> str:
    store = str(value or "").strip().lower()
    return store if store in STORES else ""


def _new_call_id() -> str:
    """``pg-`` prefix marks a console session apart from a real Vapi call id in the call log."""
    return f"pg-{uuid.uuid4().hex[:12]}"


@staff_member_required
@ensure_csrf_cookie
def playground(request):
    """The console page. ``ensure_csrf_cookie`` so the fetch POST below can send X-CSRFToken."""
    from kb.models import AgentPrompt

    prompt = AgentPrompt.objects.filter(role="faq", is_active=True).first()
    vapi_assistant_id = (prompt.vapi_assistant_id if prompt else "") or ""
    vapi_public_key = getattr(settings, "VAPI_PUBLIC_KEY", "") or ""
    return render(
        request,
        "dashboard/playground.html",
        {
            "stores": STORES,
            "probes": SCRIPTED_PROBES,
            "vapi_public_key": vapi_public_key,
            "vapi_assistant_id": vapi_assistant_id,
            # Both halves are needed for a web call: the browser key AND a provisioned assistant.
            "vapi_ready": bool(vapi_public_key and vapi_assistant_id),
        },
    )


@staff_member_required
@require_POST
def playground_send(request):
    """One console turn → the shared brain → the full diagnostic envelope + a durable log row."""
    from voice.chat import answer_text_chat

    data = _body(request)
    message = " ".join(str(data.get("message") or "").split())[:1000]
    if not message:
        return JsonResponse({"ok": False, "error": "message required"}, status=400)

    store = _safe_store(data.get("store"))
    call_id = str(data.get("call_id") or "")[:64] or _new_call_id()
    history = data.get("history") if isinstance(data.get("history"), list) else []

    started = time.monotonic()
    result = answer_text_chat(
        {
            "message": message,
            "store": store,
            "history": history,
            "phone": data.get("phone") or "",
            "session_token": call_id,
        }
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    _persist_turn(call_id, store, message, result, latency_ms)

    payload = dict(result)
    payload["call_id"] = call_id
    payload["latency_ms"] = latency_ms
    return JsonResponse(payload)


def _persist_turn(call_id: str, store: str, message: str, result: dict, latency_ms: int) -> None:
    """Write the turn into the existing call models. Best-effort: a logging failure must never
    cost the owner the answer they were testing for (same discipline as ``webhooks._log_tool_call``)."""
    from voice import guardrails
    from voice.models import VoiceCall, VoiceToolCall, VoiceTurn

    try:
        call, _ = VoiceCall.objects.get_or_create(call_id=call_id, defaults={"store": store})
        seq = call.turns.count()
        VoiceTurn.objects.create(
            call=call, seq=seq, role="user", text=guardrails.redact_pii(message)[:4000]
        )
        VoiceTurn.objects.create(
            call=call,
            seq=seq + 1,
            role="assistant",
            text=guardrails.redact_pii(str(result.get("answer") or ""))[:4000],
            latency_ms=latency_ms,
        )
        for idx, entry in enumerate(result.get("tool_results") or []):
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("tool") or "")[:64]
            VoiceToolCall.objects.update_or_create(
                call_id=call_id,
                tool_call_id=f"pg-{seq}-{idx}"[:80],
                name=name,
                defaults={
                    "args": guardrails.redact_pii(guardrails.scrub_leak(entry.get("args") or {})),
                    # result is already leak-scrubbed by dispatch; mask PII too (symmetric with args).
                    "result": guardrails.redact_pii(entry.get("result") or {}),
                    "store": store or "",
                    "source": "playground",
                },
            )
    except Exception:  # noqa: BLE001 — the console must still answer if the audit write fails
        logger.warning("playground turn log failed for %s", call_id, exc_info=True)
