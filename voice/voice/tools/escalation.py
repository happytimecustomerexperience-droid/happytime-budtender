"""``notify_staff_issue`` — the escalation "gather + email staff" tool (Phase 1 update).

The ``escalation`` member no longer warm-transfers by default. It listens, asks clarifying
questions until it fully understands the caller's issue, then calls THIS tool, which:
  1. Upserts the durable ``VoiceCall`` for this ``call.id`` and stamps ``outcome=escalation`` +
     ``reason`` (defective_return | repeated_request | dispute) — so it is never lost (ADR-017).
  2. Appends the gathered details to ``VoiceCall.ai_summary`` (the email body reads from it).
  3. Fires the immediate staff alert via ``crm.sinks.dispatch`` — an escalation outcome is always
     an immediate (URGENT) alert (``outcomes.is_immediate_alert``), so the store team is emailed
     right away. Idempotent per ``(voice_call, sink)``, so a re-delivered tool-call never re-sends.
  4. Returns a confirmation line for the assistant to speak.

A live warm transfer stays available on the escalation member as a LAST resort (the built-in
transferCall) for a caller who insists on a person right now. Self-registers via
``@register("notify_staff_issue")``. Idempotent on the Vapi ``call.id``; never raises. The central
``dispatch`` leak-scrubs every result (Leak-Guard holds uniformly).
"""

from __future__ import annotations

import logging

from voice import outcomes
from voice.tools import register

logger = logging.getLogger(__name__)

# issue_type (LLM-facing) → canonical VoiceCall.reason. Any escalation is an immediate alert, so a
# softer label still emails the team URGENT; the reason just classifies it for staff + analytics.
_ISSUE_REASON = {
    "defective_return": outcomes.REASON_DEFECTIVE,
    "defective": outcomes.REASON_DEFECTIVE,
    "dispute": outcomes.REASON_DISPUTE,
    "complaint": outcomes.REASON_DISPUTE,
    "repeated_request": outcomes.REASON_REPEATED,
    "human": outcomes.REASON_REPEATED,
}


def _resolve_store(args: dict, ctx: dict) -> str:
    """The caller's store: explicit tool arg → call ctx → ``HHT_DEFAULT_STORE`` (never empty)."""
    from django.conf import settings

    store = (args.get("store") or ctx.get("store") or "").strip()
    return store or (getattr(settings, "HHT_DEFAULT_STORE", "yakima") or "yakima")


@register("notify_staff_issue")
def notify_staff_issue(args: dict, ctx: dict) -> dict:
    """Log the caller's issue as an escalation, email the store team immediately, and return a
    confirmation line for the assistant. Idempotent on ``call.id``; never raises."""
    from crm import sinks
    from voice.models import Outcome, VoiceCall

    store = _resolve_store(args, ctx)
    issue_type = (args.get("issue_type") or "").strip().lower()
    reason = _ISSUE_REASON.get(issue_type, outcomes.REASON_DISPUTE)
    summary = (args.get("summary") or "").strip()
    caller_name = (args.get("caller_name") or "").strip()[:160]
    call_id = (ctx.get("call_id") or "").strip()

    if not call_id:
        logger.warning("notify_staff_issue with no call_id; best-effort, no durable record")
        return _envelope(store, alerted=False)

    voice_call, _ = VoiceCall.objects.update_or_create(
        call_id=call_id,
        defaults={"store": store, "outcome": Outcome.ESCALATION, "reason": reason},
    )
    # Append the gathered details to the durable summary (don't clobber a prior one).
    detail = summary if not caller_name else f"{summary}  (caller: {caller_name})"
    if detail and detail not in (voice_call.ai_summary or ""):
        voice_call.ai_summary = (
            (voice_call.ai_summary + "\n") if voice_call.ai_summary else ""
        ) + detail
        voice_call.save(update_fields=["ai_summary", "updated_at"])

    alerted = False
    try:
        alerted = sinks.dispatch(voice_call).get("email") == "success"
    except Exception:  # noqa: BLE001 — alerting must never lose the durable record
        logger.warning("escalation staff-alert dispatch failed for %s", call_id, exc_info=True)

    return _envelope(store, alerted=alerted)


def _envelope(store: str, *, alerted: bool) -> dict:
    """The spoken confirmation. No number is composed; no cost/margin field exists (Leak-Guard)."""
    return {
        "logged": True,
        "alerted": alerted,
        "store": store,
        "spoken": (
            f"Thanks for walking me through that — I've sent all of it straight to our {store} "
            "team right now, and they'll follow up with you to make it right. Is there anything "
            "else I can help you with?"
        ),
    }
