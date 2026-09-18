"""Server-to-server voice APIs used by the budtender service."""

from __future__ import annotations

import functools
import hmac
import json
import logging
import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from voice.chat import answer_text_chat
from voice.tools import dispatch

logger = logging.getLogger(__name__)

_RATE_LIMIT_WINDOW_SECONDS = 60


def _client_ip(request) -> str:
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (xff.split(",")[-1].strip() if xff else request.META.get("REMOTE_ADDR", "")) or "anon"


def _rate_limit_key(request) -> str:
    """Per (session_token, client IP) — the Bearer token is shared by the whole proxy, so
    IP alone would throttle every legitimate caller together; session_token alone lets an
    attacker rotate tokens to dodge the limit."""
    try:
        session_token = str(_body(request).get("session_token") or "")[:200]
    except Exception:  # noqa: BLE001
        session_token = ""
    return f"{session_token}:{_client_ip(request)}"


def rate_limited(scope: str):
    """Small cache-backed fixed-window throttle (env-tunable via HHT_VOICE_RATE_LIMIT,
    default 60/min). Neither /api/voice/chat nor /api/voice/kb/search carried any throttle —
    the Bearer token is a single shared proxy secret, so a leaked/compromised proxy is an
    unmetered LLM-spend and DB-growth faucet without this."""

    def deco(view):
        @functools.wraps(view)
        def wrapped(request, *a, **kw):
            limit = int(getattr(settings, "HHT_VOICE_RATE_LIMIT", 60) or 60)
            bucket = int(time.time() // _RATE_LIMIT_WINDOW_SECONDS)
            key = f"voice_rl:{scope}:{_rate_limit_key(request)}:{bucket}"
            try:
                count = cache.get_or_set(key, 0, timeout=_RATE_LIMIT_WINDOW_SECONDS)
                count = cache.incr(key)
            except ValueError:  # key expired between get_or_set and incr
                cache.set(key, 1, timeout=_RATE_LIMIT_WINDOW_SECONDS)
                count = 1
            if count > limit:
                resp = JsonResponse({"ok": False, "error": "rate_limited"}, status=429)
                resp["Retry-After"] = str(_RATE_LIMIT_WINDOW_SECONDS)
                return resp
            return view(request, *a, **kw)

        return wrapped

    return deco


_VALID_STORES = {"yakima", "mount-vernon", "pullman"}


def _authorized(request) -> bool:
    token = getattr(settings, "HHT_BACKEND_TOKEN", "") or ""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not token or not header.startswith(prefix):
        return False
    return hmac.compare_digest(header[len(prefix) :], token)


def _body(request) -> dict:
    try:
        data = json.loads(request.body or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_store(value) -> str:
    store = str(value or "").strip()
    return store if store in _VALID_STORES else ""


@csrf_exempt
@require_POST
@rate_limited("kb_search")
def kb_search(request):
    """Grounded KB lookup for sibling services. Bearer-gated; no browser access."""
    if not _authorized(request):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    data = _body(request)
    query = " ".join(str(data.get("query") or "").split())[:500]
    store = _safe_store(data.get("store"))
    if not query:
        return JsonResponse({"ok": False, "error": "query required"}, status=400)

    result = dispatch("faq_lookup", {"query": query, "store": store}, {"store": store})
    return JsonResponse({"ok": True, "result": result})


@csrf_exempt
@require_POST
@rate_limited("chat")
def text_chat(request):
    """Shared website-chat endpoint backed by the same grounded tool layer as Vapi."""
    if not _authorized(request):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    result = answer_text_chat(_body(request))
    status = 200 if result.get("ok") else 400
    return JsonResponse(result, status=status)


@csrf_exempt
@require_GET
def persona(request):
    """The owner-editable agent persona for the website chat's Vertex fallback (root project's
    ``budtender/gemini_chat.py::fetch_persona``). Bearer-gated exactly like ``text_chat``. The
    "written" AgentPrompt row is NOT a squad member — it carries the same tone/rules as the phone
    persona, phrased for text, and is never provisioned as a Vapi assistant."""
    if not _authorized(request):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    from kb.models import AgentPrompt
    from voice.provision import _with_runtime_safety, entry_greeting
    from voice.tools.faq import _looks_poisoned

    written = AgentPrompt.objects.filter(role="written", is_active=True).first()
    if not written:
        return JsonResponse({"ok": False}, status=404)

    entry = AgentPrompt.objects.filter(role="entry_router", is_active=True).first()
    updated_at = written.updated_at
    if entry and entry.updated_at > updated_at:
        updated_at = entry.updated_at

    # Screen the OWNER-EDITABLE body only — the code-owned "IMMUTABLE RUNTIME SAFETY" block
    # appended by ``_with_runtime_safety`` legitimately discusses ignoring/revealing
    # instructions (it's the refusal script) and would otherwise false-positive itself.
    if _looks_poisoned(written.body):
        # A prompt is not a security boundary, but it also must not ship poisoned instructions
        # to another model's context. The root falls back to its own safety-only instruction.
        logger.warning("refusing poisoned AgentPrompt row %s (role=written)", written.pk)
        return JsonResponse({"ok": False, "reason": "prompt_poisoned"}, status=200)

    body = _with_runtime_safety(written.body, "written")

    return JsonResponse(
        {
            "ok": True,
            "written_system_instruction": body,
            "greeting": entry_greeting(),
            "updated_at": updated_at.isoformat(),
        }
    )


# Per-store fact kinds vs. global (store="") fact kinds — matches the endpoint contract.
_STORE_KINDS = {"hours", "address", "phone"}
_GLOBAL_KINDS = {"payment", "age", "pickup"}


@csrf_exempt
@require_GET
def store_facts(request):
    """The root project's read of owner-edited store facts (persona/store-facts refresh chain,
    kb/signals.py). Bearer-gated exactly like ``text_chat``/``persona``. Only ``confirmed`` rows
    are ever surfaced (O-8 — an unconfirmed row is never spoken as fact, see
    ``StoreFact.chunk_text``), and only rows inside their validity window
    (``StoreFact.objects.current()``) — the same fail-closed gates the voice agent itself uses."""
    if not _authorized(request):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    from django.db.models import Max

    from kb.models import StoreFact
    from voice.tools.faq import _looks_poisoned

    rows = StoreFact.objects.current().filter(is_active=True, confirmed=True)
    latest = rows.aggregate(Max("updated_at"))["updated_at__max"]

    stores: dict[str, dict[str, str]] = {}
    global_facts: dict[str, str] = {}
    specials: dict[str, list[str]] = {}
    for row in rows:
        if _looks_poisoned(row.value):
            # Owner-editable content shipped verbatim to another model's context — omit the
            # row rather than serve it, and log which row so it can be reviewed/fixed.
            logger.warning(
                "omitting poisoned StoreFact row %s (store=%s, kind=%s)",
                row.pk, row.store, row.kind,
            )
            continue
        if row.kind == "special":
            if row.store:
                specials.setdefault(row.store, []).append(row.value)
            continue
        if row.store and row.kind in _STORE_KINDS:
            stores.setdefault(row.store, {})[row.kind] = row.value
        elif not row.store and row.kind in _GLOBAL_KINDS:
            global_facts[row.kind] = row.value

    return JsonResponse(
        {
            "ok": True,
            "stores": stores,
            "global": global_facts,
            "specials": specials,
            "updated_at": latest.isoformat() if latest else None,
        }
    )
