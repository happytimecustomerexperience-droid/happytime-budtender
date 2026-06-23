"""Returning-caller recognition — the margin-vs-taste switch (ADR-005/006; 11-P1 §3.4; 21-SPEC §7).

The caller's number → swedish-bot's peppered phone-hash (PII discipline) → budtender's profile
lookup (``resume-by-phone``). A HIT (``profile_summary.has_history``) marks the caller KNOWN →
budtender ranks taste-first (``W_KNOWN``); a MISS marks them anonymous → margin-first (``W_ANON``).

The phone-hash-vs-phone reconciliation (ADR-022 Option A, 21-SPEC §7.1): budtender keys
``CustomerProfile`` on a NORMALIZED RAW phone (not the peppered hash), so the lookup CALL sends the
E.164 raw number over the already-secured server-to-server Bearer/TLS channel (the existing website
pattern); the voice repo persists ONLY the peppered hash in its OWN DB (``crm.Caller`` /
``VoiceCall``). The raw number is used transiently in-request and is never written to voice storage.

``resolve_caller`` is pure-ish (one budtender call) — unit-testable with the client stubbed. It
mutates the call ``ctx`` dict in place (``session_token`` / ``known`` / ``profile_summary`` /
``caller_phone_hash`` / a transient ``_caller_phone`` used only to feed the next budtender call)
and returns it. Recognition is resolved LAZILY on first ``suggest_products`` use (11-P1 §3.4
parallel-safety note) so P1 never edits ``voice/webhooks.py`` (P2's file).
"""

from __future__ import annotations

import logging

from crm.models import phone_hash as _phone_hash

logger = logging.getLogger(__name__)


def normalize_e164(number: str) -> str:
    """A light E.164 normalization for the budtender lookup key (matches budtender's
    ``_normalize_phone`` → ``+1XXXXXXXXXX`` for US 10/11-digit). Keeps a leading ``+``; assumes US
    (+1) for a bare 10-digit number. Returns ``""`` for junk so the handshake is skipped."""
    digits = "".join(c for c in (number or "") if c.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    # Already-prefixed or international — preserve a leading + if present.
    return ("+" if (number or "").lstrip().startswith("+") else "+") + digits


def phone_hash(number: str) -> str:
    """Re-export of the peppered phone-hash (``crm.models.phone_hash``) — the returning-caller key
    persisted in the voice DB (never a raw number)."""
    return _phone_hash(number)


def resolve_caller(number: str, ctx: dict, *, client=None) -> dict:
    """Resolve the caller against budtender and stamp the recognition state onto ``ctx``.

    HIT  → ``ctx.known=True`` + ``session_token`` + non-PII ``profile_summary`` carried into
           ``suggest_products`` → ``W_KNOWN`` (taste-first).
    MISS → ``ctx.known=False`` + no ``session_token`` → ``W_ANON`` (margin-first, HIGH MARGIN).

    ``ctx['caller_phone_hash']`` is always the peppered hash (the only persisted key); the raw
    number is kept ONLY transiently on ``ctx['_caller_phone']`` to feed the budtender ``search``
    call (sent over Bearer/TLS, never persisted). A blocked/absent number → handshake skipped →
    margin-first, no error (21-SPEC §7.2)."""
    if client is None:
        from voice.budtender_client import budtender

        client = budtender()

    ctx["caller_phone_hash"] = phone_hash(number)
    ctx["recognition_resolved"] = True

    e164 = normalize_e164(number)
    if not e164:  # blocked / anonymous caller-ID → margin-first, no error
        ctx["known"] = False
        ctx["session_token"] = None
        ctx["profile_summary"] = {"has_history": False, "top_categories": [], "price_tier": ""}
        ctx["_caller_phone"] = None
        return ctx

    out = client.resume_by_phone(
        e164,
        location=ctx.get("store"),
        current_session_token=ctx.get("session_token") or f"vc-{ctx.get('call_id', '')}",
    )
    summary = out.get("profile_summary") or {"has_history": False}
    has_history = bool(summary.get("has_history"))
    ctx["profile_summary"] = summary
    ctx["known"] = has_history
    if has_history:
        # KNOWN → carry the session token + the raw phone (transient) so the next search ranks
        # W_KNOWN. The session_token alone is not yet profile-linked on a fresh call, so the
        # phone is the reliable identity field budtender resolves by (21-SPEC §6 rule 2).
        ctx["session_token"] = out.get("session_token")
        ctx["_caller_phone"] = e164
    else:
        ctx["session_token"] = None
        ctx["_caller_phone"] = None
    return ctx
