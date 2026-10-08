"""Server-to-server voice APIs used by the budtender service."""

from __future__ import annotations

import functools
import hmac
import ipaddress
import json
import logging
import re
import time

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from crm.sinks import visitor_ip
from voice import capabilities, safety_copy
from voice.chat import answer_text_chat
from voice.tools import dispatch

logger = logging.getLogger(__name__)

_RATE_LIMIT_WINDOW_SECONDS = 60
# A chat turn is one short message; nothing legitimate comes near this. Checked on the declared
# Content-Length before anything reads or parses the body (Django reads at most that many bytes).
_MAX_BODY_BYTES = 16 * 1024
# The ONLY session id this endpoint takes: the website's ``s-<base36 time>-<base36 rand>`` (as short
# as ``s-a-b``). It becomes ``VoiceCall.call_id``, and ``crm.sinks`` tells a website chat from a phone
# call by that ``s-`` shape (a chat is alert-capped, a call is not) — so any other shape could pass a
# chat off as a call, or address a real call's record by its Vapi id.
_SESSION_TOKEN_RE = re.compile(r"s-[a-z0-9]{1,11}-[a-z0-9]{1,8}")


def _client_ip(request) -> str:
    """The visitor's IP, for a request ``_authorized`` already accepted. Every website turn arrives
    from Vercel's servers, so the website-supplied ``X-HHT-Client-IP`` (set from Vercel's
    x-forwarded-for) is the visitor — trusted only because the Bearer holder sent it. Without a valid
    one: the last X-Forwarded-For hop (our own proxy's view), else REMOTE_ADDR."""
    supplied = _vouched_ip(request)
    if supplied:
        return supplied
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (xff.split(",")[-1].strip() if xff else request.META.get("REMOTE_ADDR", "")) or "anon"


def _vouched_ip(request) -> str:
    """The visitor IP the website itself reported in ``X-HHT-Client-IP``, or ``""`` when the header
    is absent or not an IP. Only this is a visitor: the fallback in ``_client_ip`` is a proxy's view,
    which for a Vercel-fronted chat is one address shared by every visitor."""
    try:
        return str(ipaddress.ip_address(request.headers.get("X-HHT-Client-IP", "").strip()))
    except ValueError:
        return ""


def _limit(name: str, default: int) -> int:
    try:
        return int(getattr(settings, name, default) or default)
    except (TypeError, ValueError):
        return default


def _over(key: str, limit: int) -> bool:
    """Count one request in this fixed window; True once ``limit`` is exceeded."""
    key = f"{key}:{int(time.time() // _RATE_LIMIT_WINDOW_SECONDS)}"
    try:
        cache.get_or_set(key, 0, timeout=_RATE_LIMIT_WINDOW_SECONDS)
        count = cache.incr(key)
    except ValueError:  # key expired between get_or_set and incr
        cache.set(key, 1, timeout=_RATE_LIMIT_WINDOW_SECONDS)
        count = 1
    return count > limit


def rate_limited(scope: str):
    """Bearer auth, a body-size cap, and a cache-backed fixed-window throttle, in that order —
    so an unauthenticated or oversized request is refused before its body is ever read or parsed.

    Two buckets per scope: per visitor IP (``HHT_VOICE_RATE_LIMIT``, default 60/min — keyed on the
    IP alone, since a session id is the caller's to rotate), and one global ceiling
    (``HHT_VOICE_GLOBAL_RATE_LIMIT``, default 600/min) so rotating IPs or tokens still cannot run up
    model spend."""

    def deco(view):
        @functools.wraps(view)
        def wrapped(request, *a, **kw):
            if not _authorized(request):
                return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)
            try:
                declared = int(request.META.get("CONTENT_LENGTH") or 0)
            except ValueError:
                declared = 0  # Django reads an unparseable Content-Length as an empty body
            if declared > _MAX_BODY_BYTES:
                return JsonResponse({"ok": False, "error": "body_too_large"}, status=413)
            if _over(f"voice_rl:{scope}:ip:{_client_ip(request)}", _limit("HHT_VOICE_RATE_LIMIT", 60)) or _over(
                f"voice_rl:{scope}:all", _limit("HHT_VOICE_GLOBAL_RATE_LIMIT", 600)
            ):
                resp = JsonResponse({"ok": False, "error": "rate_limited"}, status=429)
                resp["Retry-After"] = str(_RATE_LIMIT_WINDOW_SECONDS)
                return resp
            return view(request, *a, **kw)

        return wrapped

    return deco


_VALID_STORES = {"yakima", "mount-vernon", "pullman"}


def _authorized(request) -> bool:
    """Constant-time Bearer check. ``HHT_VOICE_TOKEN`` is the website's own secret — it opens only
    this service's endpoints. ``HHT_BACKEND_TOKEN`` is still accepted (root's own server-to-server
    calls use it), but it must never be given to the website: it also unlocks budtender's customer
    database."""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    presented = header[len(prefix) :].encode()
    tokens = [getattr(settings, name, "") or "" for name in ("HHT_VOICE_TOKEN", "HHT_BACKEND_TOKEN")]
    matches = [hmac.compare_digest(presented, token.encode()) for token in tokens if token]
    return any(matches)


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
    if not capabilities.is_enabled("channel.website_chat"):  # the owner switched chat off
        return JsonResponse(
            {"ok": True, "answer": safety_copy.CHAT_OFFLINE, "grounded": False, "disabled": True}
        )

    data = _body(request)
    # The session id becomes ``VoiceCall.call_id``: an over-long one failed the DB write silently
    # (resetting the under-21 block and the dispute carry every turn), a test prefix (``pg-``,
    # ``eval-``...) silenced staff alerts, and a Vapi-shaped one addressed a real call's record. Only
    # the website's own shape gets in (see ``_SESSION_TOKEN_RE``); the harnesses that use the test
    # prefixes call ``answer_text_chat`` in-process, never this view. An absent id stays allowed
    # (root sends none).
    session = str(data.get("session_token") or data.get("session_id") or "")
    if session and not _SESSION_TOKEN_RE.fullmatch(session):
        return JsonResponse({"ok": False, "error": "invalid session_token"}, status=400)

    # A staff alert this turn fires is counted against this visitor (crm.sinks._over_text_alert_cap).
    with visitor_ip(_vouched_ip(request)):
        result = answer_text_chat(data)
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

    updated_at = written.updated_at
    # The greeting comes from the entry agent (the concierge in single mode, else entry_router).
    for entry in AgentPrompt.objects.filter(role__in=("entry_router", "concierge"), is_active=True):
        if entry.updated_at > updated_at:
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
            "website_chat_enabled": capabilities.is_enabled("channel.website_chat"),
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
