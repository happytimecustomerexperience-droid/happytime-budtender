"""P2 — the staff-alert EmailSink + idempotent dispatch (12-P2 §7 E1-E4 + Leak-Guard).

SMTP is the locmem backend (no network). Asserts the body contract (§4.5), recipient resolution
(shared + per-store), the skipped-when-unconfigured degrade, the Leak-Guard (no cost/margin
substring), idempotency per (voice_call, sink), and that dispatch never raises.
"""

from __future__ import annotations

import pytest
from django.core import mail

from crm import sinks
from crm.models import AlertDelivery
from voice.models import VoiceCall


@pytest.fixture(autouse=True)
def _locmem(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.STAFF_ALERT_EMAIL_YAKIMA = ""
    settings.STAFF_ALERT_EMAIL_MTVERNON = ""
    settings.STAFF_ALERT_EMAIL_PULLMAN = ""
    settings.SLACK_ALERTS_ENABLED = False


def _call(**kw):
    defaults = {
        "call_id": "call_x",
        "store": "yakima",
        "caller_phone_hash": "a" * 64,
        "outcome": "escalation",
        "reason": "defective_return",
        "escalated": True,
        "human_requested_count": 2,
        "transfer_disposition": "connected",
        "transfer_number_key": "YAKIMA",
        "duration_s": 95,
        "ai_summary": "Caller reported a defective vape cart; warm-transferred to Yakima.",
    }
    defaults.update(kw)
    return VoiceCall.objects.create(**defaults)


@pytest.mark.django_db
def test_email_body_contract_and_urgent_subject():
    """E2: body carries store/outcome/reason/hash/summary/call-id; URGENT on an immediate alert."""
    vc = _call()
    sinks.dispatch(vc)
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert "URGENT" in msg.subject
    assert "yakima" in msg.subject.lower()
    body = msg.body
    assert "escalation" in body
    assert "defective_return" in body
    assert vc.caller_phone_hash[:12] in body
    assert "defective vape cart" in body
    assert vc.call_id in body
    assert "Human requested: 2" in body


@pytest.mark.django_db
def test_email_leak_guard_no_cost_or_margin():
    """E2 Leak-Guard: no `cost`/`margin` substring in the body (even if a summary tried to echo)."""
    vc = _call(ai_summary="Caller asked about a relaxing edible; staff favorite suggested.")
    sinks.dispatch(vc)
    body = mail.outbox[0].body.lower()
    assert "cost" not in body
    assert "margin" not in body


@pytest.mark.django_db
def test_email_no_raw_phone_number():
    """PII: only the hash, never a raw number, reaches the email."""
    vc = _call()
    sinks.dispatch(vc)
    assert "+1509" not in mail.outbox[0].body


@pytest.mark.django_db
def test_recipient_shared_only(settings):
    """E1: with no per-store override, the recipient is the shared STAFF_ALERT_EMAIL."""
    vc = _call(store="pullman")
    sinks.dispatch(vc)
    assert mail.outbox[0].to == ["staff@happytimeweed.com"]


@pytest.mark.django_db
def test_recipient_shared_plus_per_store(settings):
    """E1: a per-store override is ADDITIVE — both shared + per-store get the alert."""
    settings.STAFF_ALERT_EMAIL_YAKIMA = "yakima-mgr@happytimeweed.com"
    vc = _call(store="yakima")
    sinks.dispatch(vc)
    assert set(mail.outbox[0].to) == {"staff@happytimeweed.com", "yakima-mgr@happytimeweed.com"}


@pytest.mark.django_db
def test_email_skipped_when_no_recipient(settings):
    """E3: with no recipient configured, EmailSink is `skipped`, no mail sent, no raise."""
    settings.STAFF_ALERT_EMAIL = ""
    vc = _call()
    result = sinks.dispatch(vc)
    assert result["email"] == "skipped"
    assert len(mail.outbox) == 0
    assert AlertDelivery.objects.get(voice_call=vc, sink="email").status == "skipped"


@pytest.mark.django_db
def test_dispatch_idempotent_per_sink():
    """D2: a second dispatch for the same call does not re-send (the success short-circuit)."""
    vc = _call()
    sinks.dispatch(vc)
    sinks.dispatch(vc)
    assert len(mail.outbox) == 1
    assert AlertDelivery.objects.filter(voice_call=vc).count() == len(sinks.SINKS)


@pytest.mark.django_db
def test_dispatch_never_raises_on_sink_failure(monkeypatch):
    """E4 / robustness: a sink that raises is recorded `failed`, dispatch still returns a dict."""

    def _boom(self, voice_call):
        raise RuntimeError("boom")

    monkeypatch.setattr(sinks.EmailSink, "deliver", _boom)
    vc = _call()
    result = sinks.dispatch(vc)  # must not raise
    assert result["email"] == "failed"
    assert AlertDelivery.objects.get(voice_call=vc, sink="email").status == "failed"


@pytest.mark.django_db
def test_non_immediate_outcome_no_urgent(settings):
    """E1: a faq_answered call gets a non-urgent digest; Slack stays off."""
    vc = _call(outcome="faq_answered", reason="", escalated=False, transfer_disposition="")
    result = sinks.dispatch(vc)
    assert "URGENT" not in mail.outbox[0].subject
    assert result["slack"] == "skipped"  # off by default (O-9)
