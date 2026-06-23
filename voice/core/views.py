"""Core views: health check (10-P0-CHASSIS-FAQ.md §1.3 / acceptance A1)."""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse

from core.services import gemini, vapi


def healthz(request):
    """Liveness + dependency status: DB reachable + Gemini auth configured + Vapi
    auth reachable (a cheap GET /assistant?limit=1).

    200 when DB + Gemini are green AND Vapi is either green or not-yet-configured
    (an O-4 placeholder must not block liveness); 503 when a hard dependency is down
    or a configured Vapi key is unreachable.
    """
    db_ok = True
    db_error = ""
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        db_error = str(exc)

    try:
        gem = gemini.health_check()
    except Exception as exc:  # noqa: BLE001
        gem = {"mode": "error", "ready": False, "reason": str(exc)}

    try:
        vap = vapi.auth_ok()
    except Exception as exc:  # noqa: BLE001
        vap = {"ok": False, "configured": vapi.configured(), "error": str(exc)}

    # Vapi is allowed to be absent (O-4 placeholder) without flipping liveness; a
    # configured-but-unreachable Vapi key, however, is a real degradation.
    vapi_blocks = vap.get("configured", False) and not vap.get("ok", False)
    ok = db_ok and gem.get("ready", False) and not vapi_blocks
    return JsonResponse(
        {
            "status": "ok" if ok else "degraded",
            "db": {"ok": db_ok, "error": db_error},
            "gemini": gem,
            "vapi": vap,
        },
        status=200 if ok else 503,
    )
