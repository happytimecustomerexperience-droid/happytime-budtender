"""Core views: health check (10-P0-CHASSIS-FAQ.md §1.3 / acceptance A1)."""

from __future__ import annotations

import logging

from django.db import connection
from django.http import JsonResponse

from core.services import gemini, vapi
from voice.budtender_client import budtender

logger = logging.getLogger(__name__)


def healthz(request):
    """Liveness + dependency status: DB reachable + Gemini auth configured + Vapi
    auth reachable (a cheap GET /assistant?limit=1).

    200 when DB + Gemini are green AND Vapi is either green or not-yet-configured
    (an O-4 placeholder must not block liveness); 503 when a hard dependency is down
    or a configured Vapi key is unreachable.
    """
    db_ok = True
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        logger.warning("healthz DB check failed: %s", type(exc).__name__)

    try:
        gem = gemini.health_check()
    except Exception as exc:  # noqa: BLE001
        logger.warning("healthz Gemini check failed: %s", type(exc).__name__)
        gem = {"ready": False}

    try:
        vap = vapi.auth_ok()
    except Exception as exc:  # noqa: BLE001
        logger.warning("healthz Vapi check failed: %s", type(exc).__name__)
        vap = {"ok": False, "configured": vapi.configured()}

    try:
        bt = budtender()
        bud = {"ok": bt.health(), "configured": bool(bt.base_url)}
    except Exception as exc:  # noqa: BLE001
        logger.warning("healthz budtender check failed: %s", type(exc).__name__)
        bud = {"ok": False, "configured": False}

    # Vapi is allowed to be absent (O-4 placeholder) without flipping liveness; a
    # configured-but-unreachable Vapi key, however, is a real degradation.
    vapi_blocks = vap.get("configured", False) and not vap.get("ok", False)
    budtender_blocks = bud.get("configured", False) and not bud.get("ok", False)
    ok = db_ok and gem.get("ready", False) and not vapi_blocks and not budtender_blocks
    safe_vapi = {"ok": bool(vap.get("ok")), "configured": bool(vap.get("configured"))}
    safe_budtender = {"ok": bool(bud.get("ok")), "configured": bool(bud.get("configured"))}
    return JsonResponse(
        {
            "status": "ok" if ok else "degraded",
            "db": {"ok": db_ok},
            "gemini": {"ready": bool(gem.get("ready"))},
            "vapi": safe_vapi,
            "budtender": safe_budtender,
        },
        status=200 if ok else 503,
    )
