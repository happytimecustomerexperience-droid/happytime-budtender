"""Vapi webhook signature verification — fail-closed at the edge (ADR-019; 23-SPEC §3.1).

The webhook gate. ``verify_signature(request)`` is called FIRST inside ``voice/webhooks.py``
(NOT middleware, so a bad signature returns a Vapi-shaped 401 — a middleware 401 confuses
Vapi's retry; 10-P0 §3.2). Two modes, both constant-time (``hmac.compare_digest``), both
reject-by-default:

  * Mode A — HMAC body signature: ``X-Vapi-Signature: hex(hmac_sha256(secret, raw_body))``
    (preferred when Vapi sends it).
  * Mode B — shared-secret echo: ``X-Vapi-Secret: <VAPI_WEBHOOK_SECRET>`` (Vapi echoes the
    assistant/tool ``server.secret``).

Fail-closed posture (23-SPEC §4.1): an unconfigured secret, a missing header, or a wrong
proof → reject. The exact Vapi header literal is an O-placeholder pinned in
``20-SPEC-vapi-deploy.md``; the header NAMES are env-driven (``VAPI_SIGNATURE_HEADER`` /
``VAPI_SECRET_HEADER``) so a header change is config, not code. The constant-time idiom mirrors
``crm.models.phone_hash``'s peppered-compare discipline + budtender ``auth.py``'s fail-closed Bearer.

``compute_signature`` is reused by the P5 load-test (``tools/loadtest_voice.py``) so the load
test signs exactly like Vapi — one signing function, two callers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


def compute_signature(raw_body: bytes, secret: str) -> str:
    """Hex HMAC-SHA256 over the raw request body with the shared secret (Mode A proof)."""
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(request) -> tuple[bool, str]:
    """Authenticate an inbound Vapi webhook. Returns ``(ok, reason)``; ``ok=False`` means the
    caller must reject with 401 BEFORE parsing the body (fail-closed).

    Order: unconfigured-secret → reject; Mode-A signature header present → HMAC compare; else
    Mode-B secret header present → constant-time compare; else no proof → reject. Every compare
    is ``hmac.compare_digest`` (never ``==`` on a secret); the secret is NEVER logged."""
    secret = getattr(settings, "VAPI_WEBHOOK_SECRET", "") or ""
    if not secret:
        # Fail closed: an unconfigured secret rejects rather than opens the gate (23-SPEC §4.1).
        return False, "webhook secret not configured"

    sig_header = getattr(settings, "VAPI_SIGNATURE_HEADER", "X-Vapi-Signature")
    secret_header = getattr(settings, "VAPI_SECRET_HEADER", "X-Vapi-Secret")

    # Mode A — HMAC body signature (preferred when present).
    sig = request.headers.get(sig_header, "")
    if sig:
        # Reading request.body caches it on the request; the view's later parse is free.
        expected = compute_signature(request.body, secret)
        return (hmac.compare_digest(expected, sig), "bad hmac signature")

    # Mode B — shared-secret echo header.
    provided = request.headers.get(secret_header, "")
    if provided:
        return (hmac.compare_digest(provided, secret), "bad shared secret")

    return False, "no signature header"
