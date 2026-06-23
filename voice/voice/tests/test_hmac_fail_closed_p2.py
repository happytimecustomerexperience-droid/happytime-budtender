"""P2 — HMAC fail-closed on the eocr branch (12-P2 §7 F1, MANDATORY GATE).

A missing/bad Vapi signature → 401 BEFORE end_of_call_report runs: no VoiceCall written, no email
sent. A valid signature lets the handler run. Re-asserts the P0 gate on the P2 event so the durable
write + email path can never be triggered by a forged eocr.
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


def _eocr_payload():
    return {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": "call_forged", "customer": {"number": "+15095551212"}},
            "endedReason": "assistant-forwarded-call",
            "durationSeconds": 50,
            "transcript": "User: my cart is defective.",
            "messages": [{"role": "user", "message": "defective"}],
        }
    }


@pytest.mark.django_db
def test_forged_eocr_no_signature_rejected(client):
    """No signature → 401, NO VoiceCall written, NO email sent."""
    from django.core import mail

    from voice.models import VoiceCall

    raw = json.dumps(_eocr_payload()).encode()
    resp = client.post(WEBHOOK_URL, data=raw, content_type="application/json")
    assert resp.status_code == 401
    assert VoiceCall.objects.filter(call_id="call_forged").count() == 0
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_forged_eocr_bad_signature_rejected(client):
    """A wrong HMAC signature → 401, no record, no email."""
    from django.core import mail

    from voice.models import VoiceCall

    raw = json.dumps(_eocr_payload()).encode()
    resp = client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SIGNATURE": "deadbeef" * 8},
    )
    assert resp.status_code == 401
    assert VoiceCall.objects.filter(call_id="call_forged").count() == 0
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_valid_signature_runs_handler(client):
    """A correct signature → 200 + the durable record + the staff email (the gate lets it through)."""
    from django.core import mail

    from voice.models import VoiceCall

    raw = json.dumps(_eocr_payload()).encode()
    sig = signing.compute_signature(raw, SECRET)
    resp = client.post(
        WEBHOOK_URL, data=raw, content_type="application/json", **{"HTTP_X_VAPI_SIGNATURE": sig}
    )
    assert resp.status_code == 200
    vc = VoiceCall.objects.get(call_id="call_forged")
    assert vc.outcome == "escalation"
    assert vc.reason == "defective_return"
    assert len(mail.outbox) == 1
