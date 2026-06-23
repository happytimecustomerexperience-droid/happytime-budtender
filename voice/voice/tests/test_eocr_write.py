"""P2 — the synchronous durable eocr write (12-P2 §7 D1/D2/D3).

The end-of-call-report handler writes the durable VoiceCall FIRST (record never lost), classifies
the outcome, then dispatches the staff alert. Idempotent on call_id; the record survives an email
failure. All external calls mocked (Gemini off via conftest; SMTP locmem / monkeypatched-raise).
"""

from __future__ import annotations

import json

import pytest

from voice import signing

WEBHOOK_URL = "/api/voice/vapi"
SECRET = "test-webhook-secret-0123456789"


@pytest.fixture(autouse=True)
def _cfg(settings):
    settings.VAPI_WEBHOOK_SECRET = SECRET
    settings.VAPI_SIGNATURE_HEADER = "X-Vapi-Signature"
    settings.VAPI_SECRET_HEADER = "X-Vapi-Secret"
    settings.HHT_DEFAULT_STORE = "yakima"
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def _post(client, payload):
    raw = json.dumps(payload).encode()
    sig = signing.compute_signature(raw, SECRET)
    return client.post(
        WEBHOOK_URL, data=raw, content_type="application/json", **{"HTTP_X_VAPI_SIGNATURE": sig}
    )


def _defective_eocr(call_id="call_def_1"):
    return {
        "message": {
            "type": "end-of-call-report",
            "call": {
                "id": call_id,
                "customer": {"number": "+15095551212"},
                "assistantId": "asst_esc",
            },
            "endedReason": "assistant-forwarded-call",
            "durationSeconds": 95,
            "transcript": "User: my vape cart is defective and won't fire, I want a refund.",
            "messages": [{"role": "user", "message": "my cart is defective"}],
            "destination": {"type": "number", "number": "+15095711106"},
            "analysis": {"structuredData": {"store": "yakima", "reason": "defective_return"}},
        }
    }


@pytest.mark.django_db
def test_eocr_writes_durable_escalation_record(client):
    """D1: a defective-return eocr → a VoiceCall with the right outcome/reason/transfer + hash."""
    from django.core import mail

    from voice.models import VoiceCall

    resp = _post(client, _defective_eocr())
    assert resp.status_code == 200

    vc = VoiceCall.objects.get(call_id="call_def_1")
    assert vc.outcome == "escalation"
    assert vc.reason == "defective_return"
    assert vc.escalated is True
    assert vc.transfer_disposition == "connected"
    assert vc.transfer_number_key == "YAKIMA"
    assert vc.duration_s == 95
    # PII: the raw number appears nowhere on the row; only the 64-hex hash.
    assert len(vc.caller_phone_hash) == 64
    assert "+15095551212" not in (vc.transcript + vc.caller_phone_hash)
    # The immediate alert email fired (locmem) with the URGENT subject.
    assert len(mail.outbox) == 1
    assert "URGENT" in mail.outbox[0].subject


@pytest.mark.django_db
def test_eocr_idempotent_no_dup_row_or_email(client):
    """D2: re-delivering the same eocr → one VoiceCall, one successful email AlertDelivery."""
    from django.core import mail

    from crm.models import AlertDelivery
    from voice.models import VoiceCall

    _post(client, _defective_eocr())
    _post(client, _defective_eocr())  # Vapi retry

    assert VoiceCall.objects.filter(call_id="call_def_1").count() == 1
    vc = VoiceCall.objects.get(call_id="call_def_1")
    assert AlertDelivery.objects.filter(voice_call=vc, sink="email", status="success").count() == 1
    # Exactly one email despite two deliveries (the AlertDelivery short-circuit).
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_record_survives_email_failure(client, monkeypatch):
    """D3: with the EmailSink raising, the VoiceCall is still written + the webhook returns 200;
    the email AlertDelivery is recorded `failed` (logged, not fatal)."""
    from crm import sinks
    from crm.models import AlertDelivery
    from voice.models import VoiceCall

    def _boom(self, voice_call):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(sinks.EmailSink, "deliver", _boom)

    resp = _post(client, _defective_eocr("call_def_2"))
    assert resp.status_code == 200

    vc = VoiceCall.objects.get(call_id="call_def_2")  # record survived
    assert vc.outcome == "escalation"
    assert AlertDelivery.objects.get(voice_call=vc, sink="email").status == "failed"


@pytest.mark.django_db
def test_plain_call_emails_non_urgent_digest(client):
    """E1 control: every call gets a per-call digest; a plain FAQ call is NOT urgent."""
    from django.core import mail

    from voice.models import VoiceCall

    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "call_faq_1", "customer": {"number": "+15095550000"}},
            "endedReason": "customer-ended-call",
            "durationSeconds": 30,
            "transcript": "User: what time do you close? AI: 11 PM.",
            "messages": [{"role": "user", "message": "what time do you close"}],
        }
    }
    resp = _post(client, payload)
    assert resp.status_code == 200
    vc = VoiceCall.objects.get(call_id="call_faq_1")
    assert vc.outcome == "faq_answered"
    assert vc.escalated is False
    assert len(mail.outbox) == 1
    assert "URGENT" not in mail.outbox[0].subject
