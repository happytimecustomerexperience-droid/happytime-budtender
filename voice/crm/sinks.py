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

from django.conf import settings
from django.core.mail import send_mail

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
        reason_line = f"  (reason: {voice_call.reason})" if voice_call.reason else ""
        transfer = (
            f"{voice_call.transfer_disposition or '—'} ({voice_call.transfer_number_key or '—'})"
        )
        body = (
            f"New voice call — {voice_call.store or '—'}.\n"
            f"Outcome: {voice_call.outcome or '—'}{reason_line}\n"
            f"Caller (hashed): {(voice_call.caller_phone_hash or '—')[:12]}…\n"
            f"Duration: {voice_call.duration_s or '—'}s\n"
            f"Human requested: {voice_call.human_requested_count}×\n"
            f"Transfer: {transfer}\n\n"
            f"Summary:\n{voice_call.ai_summary or '(none)'}\n\n"
            f"Call id: {voice_call.call_id}   ·   logged {voice_call.created_at}\n"
        )
        send_mail(
            subject=subject[:120],
            message=body,
            from_email=getattr(settings, "LEAD_EMAIL_FROM", "bot@happytimeweed.com"),
            recipient_list=recipients,
            fail_silently=False,
        )


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
            "reason": voice_call.reason or "",
            "summary": voice_call.ai_summary or "(no summary)",
            "call_id": voice_call.call_id,
        }
        data = json.dumps({"text": json.dumps(block)}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 (config-supplied URL)
            if r.status >= 300:
                raise RuntimeError(f"slack HTTP {r.status}")


SINKS: list[Sink] = [DBSink(), EmailSink(), SlackSink()]


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
