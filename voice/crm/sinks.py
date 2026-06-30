"""Per-call staff-alert sinks for the voice repo (12-P2 §3.2 / §4.5; ADR-017).

Ported from swedish-bot/crm/sinks.py (EmailSink + the independent + idempotent ``dispatch``
pattern), retargeted from ``service_request`` → ``VoiceCall``. The durable ``VoiceCall`` row IS the
record (``DBSink`` is a no-op); ``EmailSink`` sends a per-call digest to ``STAFF_ALERT_EMAIL`` —
with an ``— URGENT`` subject on an immediate-alert outcome (escalation / vendor / defective). Each
sink is independent (one failing never blocks the others) and ``dispatch`` is **idempotent** per
``(voice_call, sink)`` via the ``AlertDelivery`` ledger, so a re-delivered eocr (Vapi retries) never
re-sends an email. ``dispatch`` never raises — a sink failure is recorded ``failed``, never fatal.

Slack is the optional secondary sink (off until ``SLACK_ALERTS_ENABLED`` + ``SLACK_WEBHOOK_URL``,
O-9) and only fires on an immediate alert — the durable ``VoiceCall`` + email are authoritative.

Leak-Guard (12-P2 §4.5): the email body is built ONLY from ``VoiceCall`` fields + ``ai_summary`` —
no product cost/margin field exists on the row; a contract test asserts no ``cost``/``margin``
substring. PII: the hashed caller, never the raw number.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from html import escape

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from voice import outcomes

logger = logging.getLogger(__name__)


def _recipients_for(store: str) -> list[str]:
    """Recipient list for a store's alert: the shared ``STAFF_ALERT_EMAIL`` PLUS any per-store
    override (additive, not replacing — 12-P2 §9). De-duplicated, order-stable."""
    shared = getattr(settings, "STAFF_ALERT_EMAIL", "") or ""
    per_store = {
        "yakima": getattr(settings, "STAFF_ALERT_EMAIL_YAKIMA", ""),
        "mount-vernon": getattr(settings, "STAFF_ALERT_EMAIL_MTVERNON", ""),
        "pullman": getattr(settings, "STAFF_ALERT_EMAIL_PULLMAN", ""),
    }.get(store, "")
    out: list[str] = []
    for addr in (shared, per_store):
        if addr and addr not in out:
            out.append(addr)
    return out


def _is_immediate(voice_call) -> bool:
    """Whether this call warrants an immediate (URGENT) alert — escalation/vendor/defective."""
    return outcomes.is_immediate_alert(voice_call.outcome or "", voice_call.reason or "")


def _safe_text(value, default: str = "") -> str:
    """Email-safe text with the same no-cost/no-margin wall as spoken tool results."""
    from voice import guardrails

    text = str(value or default)
    scrubbed = guardrails.scrub_leak(text)
    if isinstance(scrubbed, dict):
        return "[redacted: leak blocked]"
    return scrubbed


def _conversation_lines(voice_call) -> list[str]:
    """Full conversation log for staff: VoiceTurn rows first, transcript fallback second."""
    turns = list(voice_call.turns.order_by("seq"))
    if turns:
        lines = []
        for t in turns:
            text = _safe_text(t.text)
            tool = _safe_text(t.tool_name)
            if not text and not tool:
                continue
            label = (t.role or "turn").upper()
            if tool:
                label = f"{label} [{tool}]"
            lines.append(f"{label}: {text or '(tool call)'}")
        return lines
    transcript = _safe_text(getattr(voice_call, "transcript", ""))
    return [transcript] if transcript else ["(no transcript captured)"]


def _text_body(voice_call, transfer: str, reason_line: str) -> str:
    conversation = "\n".join(_conversation_lines(voice_call))
    return (
        f"New voice call - {voice_call.store or '-'}.\n"
        f"Outcome: {voice_call.outcome or '-'}{reason_line}\n"
        f"Caller (hashed): {(voice_call.caller_phone_hash or '-')[:12]}...\n"
        f"Duration: {voice_call.duration_s or '-'}s\n"
        f"Human requested: {voice_call.human_requested_count}x\n"
        f"Transfer: {transfer}\n\n"
        f"Summary:\n{_safe_text(voice_call.ai_summary, '(none)')}\n\n"
        f"Conversation log:\n{conversation}\n\n"
        f"Call id: {voice_call.call_id}   logged {voice_call.created_at}\n"
    )


def _html_body(voice_call, transfer: str, reason_line: str, immediate: bool) -> str:
    rows = []
    for line in _conversation_lines(voice_call):
        role, _, text = line.partition(":")
        rows.append(
            f"<tr><td>{escape(role)}</td><td>{escape(text.strip() if text else role)}</td></tr>"
        )
    badge = "URGENT" if immediate else "Call"
    return f"""<!doctype html>
<html>
  <body style="font-family:Arial,sans-serif;color:#1f2933;line-height:1.45">
    <h2>Happy Time voice alert</h2>
    <p><strong>{escape(badge)}</strong> - {escape(voice_call.store or 'store')} - {escape(voice_call.outcome or 'call')}</p>
    <table cellpadding="6" cellspacing="0" style="border-collapse:collapse">
      <tr><td><strong>Reason</strong></td><td>{escape(reason_line.strip() or '-')}</td></tr>
      <tr><td><strong>Caller hash</strong></td><td>{escape((voice_call.caller_phone_hash or '-')[:12])}</td></tr>
      <tr><td><strong>Duration</strong></td><td>{escape(str(voice_call.duration_s or '-'))}s</td></tr>
      <tr><td><strong>Human requested</strong></td><td>{voice_call.human_requested_count}x</td></tr>
      <tr><td><strong>Transfer</strong></td><td>{escape(transfer)}</td></tr>
      <tr><td><strong>Call id</strong></td><td>{escape(voice_call.call_id)}</td></tr>
    </table>
    <h3>Summary</h3>
    <p>{escape(_safe_text(voice_call.ai_summary, '(none)'))}</p>
    <h3>Conversation log</h3>
    <table cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%">
      <tr><th align="left">Role</th><th align="left">Message</th></tr>
      {''.join(rows)}
    </table>
  </body>
</html>"""


class Sink:
    name = "base"

    def enabled(self, voice_call) -> bool:
        return True

    def deliver(self, voice_call) -> None:
        raise NotImplementedError


class DBSink(Sink):
    """The durable ``VoiceCall`` row IS the record — already written synchronously by the eocr
    handler. Always succeeds (the idempotency boundary is the unique ``call_id``)."""

    name = "db"

    def deliver(self, voice_call) -> None:
        return None


class EmailSink(Sink):
    name = "email"

    def enabled(self, voice_call) -> bool:
        return bool(_recipients_for(voice_call.store))

    def deliver(self, voice_call) -> None:
        recipients = _recipients_for(voice_call.store)
        immediate = _is_immediate(voice_call)
        urgent = " — URGENT" if immediate else ""
        subject = (
            f"[Happy Time voice] {voice_call.store or 'store'} — "
            f"{voice_call.outcome or 'call'}{urgent}"
        )
        reason_line = f"  (reason: {_safe_text(voice_call.reason)})" if voice_call.reason else ""
        transfer = (
            f"{voice_call.transfer_disposition or '—'} ({voice_call.transfer_number_key or '—'})"
        )
        body = _text_body(voice_call, transfer, reason_line)
        msg = EmailMultiAlternatives(
            subject=subject[:120],
            body=body,
            from_email=getattr(settings, "LEAD_EMAIL_FROM", "bot@happytimeweed.com"),
            to=recipients,
        )
        msg.attach_alternative(_html_body(voice_call, transfer, reason_line, immediate), "text/html")
        msg.send(fail_silently=False)


class SlackSink(Sink):
    name = "slack"

    def enabled(self, voice_call) -> bool:
        # Off by default (O-9); fires ONLY on an immediate alert when configured + enabled.
        return bool(
            getattr(settings, "SLACK_ALERTS_ENABLED", False)
            and getattr(settings, "SLACK_WEBHOOK_URL", "")
            and _is_immediate(voice_call)
        )

    def deliver(self, voice_call) -> None:
        url = settings.SLACK_WEBHOOK_URL
        block = {
            "store": voice_call.store or "store",
            "outcome": voice_call.outcome or "call",
            "reason": _safe_text(voice_call.reason),
            "summary": _safe_text(voice_call.ai_summary, "(no summary)"),
            "call_id": voice_call.call_id,
        }
        data = json.dumps({"text": json.dumps(block)}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (config-supplied URL)
            if r.status >= 300:
                raise RuntimeError(f"slack HTTP {r.status}")


class N8nSink(Sink):
    """POST a leak-safe call event to a configured n8n webhook (P6). Fires on EVERY call when
    ``N8N_WEBHOOK_URL`` is set (the credentials editor surfaces it) — n8n owns the downstream
    automation (CRM sync, SMS, sheets, etc.). Leak-safe: VoiceCall carries no cost/margin; the
    caller is the peppered hash, never a raw number (PII discipline)."""

    name = "n8n"

    def enabled(self, voice_call) -> bool:
        # The credentials editor applies N8N_WEBHOOK_URL to settings (and os.environ) on save.
        return bool(getattr(settings, "N8N_WEBHOOK_URL", ""))

    def deliver(self, voice_call) -> None:
        url = settings.N8N_WEBHOOK_URL
        payload = {
            "event": "voice_call",
            "call_id": voice_call.call_id,
            "store": voice_call.store or "",
            "outcome": voice_call.outcome or "",
            "reason": voice_call.reason or "",
            "escalated": bool(voice_call.escalated),
            "human_requested": voice_call.human_requested_count,
            "duration_s": voice_call.duration_s,
            "caller_hash": (voice_call.caller_phone_hash or "")[:16],
            "suggested_skus": list(voice_call.suggested_skus or []),
            "summary": _safe_text(voice_call.ai_summary, ""),
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (config-supplied URL)
            if r.status >= 300:
                raise RuntimeError(f"n8n HTTP {r.status}")


SINKS: list[Sink] = [DBSink(), EmailSink(), SlackSink(), N8nSink()]


def dispatch(voice_call) -> dict[str, str]:
    """Fire every sink independently for one VoiceCall, idempotent per ``(voice_call, sink)``.

    Records one ``AlertDelivery`` row per sink; a row already ``success`` short-circuits (so a
    re-delivered eocr never re-sends). Returns ``{sink_name: status}``. Never raises — a sink
    failure is logged + recorded ``failed``, never fatal (the durable record is already safe)."""
    from crm.models import AlertDelivery

    results: dict[str, str] = {}
    for sink in SINKS:
        delivery, _ = AlertDelivery.objects.get_or_create(voice_call=voice_call, sink=sink.name)
        if delivery.status == "success":
            results[sink.name] = "success"  # idempotent: already delivered
            continue
        delivery.attempts += 1
        if not sink.enabled(voice_call):
            delivery.status = "skipped"
            delivery.last_error = "disabled or not configured"
        else:
            try:
                sink.deliver(voice_call)
                delivery.status = "success"
                delivery.last_error = ""
            except Exception as exc:  # noqa: BLE001
                delivery.status = "failed"
                delivery.last_error = str(exc)[:500]
                logger.warning("voice sink %s failed: %s", sink.name, exc)
        delivery.save()
        results[sink.name] = delivery.status
    return results
