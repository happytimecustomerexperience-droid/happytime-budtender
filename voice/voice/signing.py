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

# Body size cap — an unauthenticated caller could otherwise force the server to read and HMAC
# an arbitrarily large body before rejecting it (DoS via memory/CPU). Checked against
# ``CONTENT_LENGTH`` (a header) BEFORE ``request.body`` is ever touched, so an oversized body is
# never read into memory here.
MAX_BODY_BYTES = 256 * 1024


def _body_too_large(request) -> bool:
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        return False
    return length > MAX_BODY_BYTES


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

    if _body_too_large(request):
        # Checked via CONTENT_LENGTH only — request.body is never read for an oversized request.
        return False, "body too large"

    sig_header = getattr(settings, "VAPI_SIGNATURE_HEADER", "X-Vapi-Signature")
    secret_header = getattr(settings, "VAPI_SECRET_HEADER", "X-Vapi-Secret")

    # Mode A — HMAC body signature (preferred when present).
    sig = request.headers.get(sig_header, "")
    if sig:
        # Reading request.body caches it on the request; the view's later parse is free.
        expected = compute_signature(request.body, secret)
        if not hmac.compare_digest(expected, sig):
            return False, "bad hmac signature"
        if _is_replay(sig):
            return False, "replayed signature"
        return True, ""

    # Mode B — shared-secret echo header.
    provided = request.headers.get(secret_header, "")
    if provided:
        if not hmac.compare_digest(provided, secret):
            return False, "bad shared secret"
        if _is_replay(compute_signature(request.body, secret)):
            return False, "replayed signature"
        return True, ""

    return False, "no signature header"


# ── Replay protection (23-SPEC T2) ──────────────────────────────────────────────
# Vapi does not document a signed timestamp/nonce header on this webhook (checked
# 20-SPEC-vapi-deploy.md + the Vapi server-message docs), so this is a best-effort seen-proof
# cache: a valid proof (the HMAC signature itself — already a body-bound, secret-keyed value)
# is counted for a bounded window. Vapi's own at-least-once delivery legitimately resends the
# SAME signed body a small number of times (the app's handlers are separately idempotent on
# call_id/tool_call_id — ADR: "the bus is at-least-once, every payload carries an idempotency
# key"), so this does NOT reject on first repeat; it caps how many times any one captured proof
# can be replayed, closing the "capture once, replay forever/flood" gap without breaking a
# normal retry of a legitimate event.
_REPLAY_TTL_SECONDS = 600
_REPLAY_MAX_USES = 3
_REPLAY_CACHE_PREFIX = "vapi_webhook_seen:"


def _is_replay(proof: str) -> bool:
    """Count one use of ``proof``; return True once it has been seen more than
    ``_REPLAY_MAX_USES`` times within the TTL window."""
    from django.core.cache import cache

    key = _REPLAY_CACHE_PREFIX + hashlib.sha256(proof.encode()).hexdigest()
    if cache.add(key, 1, timeout=_REPLAY_TTL_SECONDS):
        return False  # first time this proof has been seen
    try:
        count = cache.incr(key)
    except ValueError:  # key expired between add() and incr() — treat as first use
        cache.set(key, 1, timeout=_REPLAY_TTL_SECONDS)
        return False
    return count > _REPLAY_MAX_USES
