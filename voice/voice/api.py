"""Server-to-server voice APIs used by the budtender service."""

from __future__ import annotations

import hmac
import json

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from voice.chat import answer_text_chat
from voice.tools import dispatch

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
def text_chat(request):
    """Shared website-chat endpoint backed by the same grounded tool layer as Vapi."""
    if not _authorized(request):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    result = answer_text_chat(_body(request))
    status = 200 if result.get("ok") else 400
    return JsonResponse(result, status=status)
