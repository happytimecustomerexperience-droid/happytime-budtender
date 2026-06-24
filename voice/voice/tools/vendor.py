"""``notify_vendor_callback`` — the vendor no-answer callback tool (13-P3 §3.1 / §4.2; ADR-015).

The FALLBACK leg of the vendor flow: the ``vendor`` member warm-transfers to the store human FIRST;
only when NO ONE ANSWERS (control returns to the AI member) does the model capture the reason and
call this tool. It is therefore never the first action in the flow (B3) — the warm transfer is.

What it does, in order (the durable record is written FIRST so it is never lost — ADR-017):
  1. Resolve the store (arg → ctx → HHT_DEFAULT_STORE) + fold the reason to the stable enum.
  2. Upsert the ``VoiceCall`` for this ``call_id`` and stamp ``outcome=vendor_callback`` /
     ``reason="vendor"`` (correct the instant the tool runs; P2's classifier also recognizes the
     label belt-and-suspenders).
  3. ``get_or_create`` an idempotent ``VendorCallback`` keyed on ``vapi_call_id`` — a re-delivered
     tool-call returns the SAME ``callback_id``, creates no 2nd row, and does NOT re-fire the email.
  4. Fire the immediate staff alert via ``crm.sinks.dispatch`` (idempotent per (call, sink) ledger).
  5. Return the §4.2 envelope with the config/KB callback window (Numbers-Guard — the only number,
     and it is config-sourced; the LLM speaks it, never invents it).

Self-registers via ``@register("notify_vendor_callback")``. The central ``dispatch`` leak-scrubs
every result; Leak-Guard + Numbers-Guard hold uniformly across all tools (ADR-008/012). PII: only
the peppered ``caller_phone_hash`` is persisted — the raw number is hashed in-request, never stored.
"""

from __future__ import annotations

import logging

from voice import vendor_flow
from voice.tools import register

logger = logging.getLogger(__name__)


def _resolve_store(args: dict, ctx: dict) -> str:
    """The caller's store: explicit tool arg → call ctx → ``HHT_DEFAULT_STORE`` (never empty)."""
    from django.conf import settings

    store = (args.get("store") or ctx.get("store") or "").strip()
    return store or (getattr(settings, "HHT_DEFAULT_STORE", "yakima") or "yakima")


def _callback_window() -> str:
    """The spoken callback window from config (Numbers-Guard). Reads
    ``settings.HHT_VENDOR_CALLBACK_WINDOW``; falls back to the documented default. Never the LLM."""
    from django.conf import settings

    return vendor_flow.callback_window_text(getattr(settings, "HHT_VENDOR_CALLBACK_WINDOW", ""))


def _phone_hash_from(args: dict, ctx: dict) -> str:
    """The peppered caller hash — a server-derived value wins (from the transient ctx number); an
    explicitly-passed hash is honored only if no number is in ctx. The raw number is never stored."""
    from crm.models import phone_hash

    number = ctx.get("caller_number") or ""
    if number:
        return phone_hash(number)
    # No raw number in ctx → accept a caller-supplied hash if it looks like one (never a raw number).
    supplied = (args.get("caller_phone_hash") or "").strip()
    return supplied


@register("notify_vendor_callback")
def notify_vendor_callback(args: dict, ctx: dict) -> dict:
    """Log a vendor callback after a no-answer transfer, alert staff, and return the callback
    window the assistant states (§4.2). Idempotent on the Vapi ``call.id``; never raises."""
    from crm.models import VendorCallback
    from voice.models import Outcome, VoiceCall

    store = _resolve_store(args, ctx)
    reason = vendor_flow.normalize_reason(args.get("reason") or args.get("summary") or "")
    summary = (args.get("summary") or "").strip()
    caller_name = (args.get("caller_name") or "").strip()[:128]
    caller_phone_hash = _phone_hash_from(args, ctx)
    window = _callback_window()
    call_id = (ctx.get("call_id") or "").strip()

    # No addressable call id → still answer the caller (best-effort), but we cannot persist an
    # idempotent record. This never happens on a real Vapi tool-call (call.id is always present).
    if not call_id:
        logger.warning("notify_vendor_callback with no call_id; returning best-effort window")
        return _envelope(None, store, reason, window, alerted=False)

    # (1+2) durable VoiceCall upsert + the outcome stamp (correct the instant the tool runs).
    voice_call, _ = VoiceCall.objects.update_or_create(
        call_id=call_id,
        defaults={
            "store": store,
            "outcome": Outcome.VENDOR_CALLBACK,
            "reason": "vendor",
        },
    )
    if caller_phone_hash and not voice_call.caller_phone_hash:
        voice_call.caller_phone_hash = caller_phone_hash
        voice_call.save(update_fields=["caller_phone_hash", "updated_at"])

    # (3) idempotent VendorCallback — a re-delivered tool-call returns the SAME row + no 2nd alert.
    callback, created = VendorCallback.objects.get_or_create(
        vapi_call_id=call_id,
        defaults={
            "voice_call": voice_call,
            "store": store,
            "reason": reason,
            "summary": summary,
            "caller_name": caller_name,
            "caller_phone_hash": caller_phone_hash,
            "callback_window": window,
        },
    )

    # (4) immediate staff alert — ONLY on first creation (the dispatch ledger is itself idempotent,
    # but skipping it on a duplicate keeps the envelope's ``alerted`` honest: the alert already went).
    alerted = False
    if created:
        alerted = _alert_staff(voice_call)
        if alerted and not callback.alerted:
            callback.alerted = True
            callback.save(update_fields=["alerted", "updated_at"])

    return _envelope(callback, store, reason, window, alerted=alerted)


def _alert_staff(voice_call) -> bool:
    """Fire the per-call staff alert immediately. The durable record is already safe, so an alert
    failure is logged + non-fatal (never loses the callback — ADR-017). Returns whether the email
    sink delivered (so the envelope's ``alerted`` reflects reality)."""
    try:
        from crm import sinks

        results = sinks.dispatch(voice_call)
        return results.get("email") == "success"
    except Exception:  # noqa: BLE001 — alerting must never lose the durable record
        logger.warning(
            "vendor staff-alert dispatch failed for %s", voice_call.call_id, exc_info=True
        )
        return False


def _envelope(callback, store: str, reason: str, window: str, *, alerted: bool) -> dict:
    """The frozen §4.2 tool-result body. The window is the ONLY number and it is config-sourced
    (Numbers-Guard). No cost/margin field exists (Leak-Guard holds uniformly). The spoken line uses
    the readable store name (never the raw slug); the ``store`` field keeps the slug for logging."""
    from voice import constants as C

    return {
        "logged": True,
        "callback_id": callback.pk if callback else None,
        "callback_window": window,
        "store": store,
        "reason": reason,
        "alerted": alerted,
        "spoken": (
            f"Got it — I've let the {C.spoken_store(store)} team know and someone will call you back "
            f"within {window}."
        ),
    }
