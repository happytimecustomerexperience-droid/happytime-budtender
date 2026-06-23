"""P3 — HMAC fail-closed on the vendor tool-call (13-P3 §7 H1; MANDATORY GATE).

A ``tool-calls`` payload carrying notify_vendor_callback with a missing/bad signature → 401 BEFORE
the handler runs: no VendorCallback written, no VoiceCall written, no email. A valid signature lets
it through + logs the callback. Re-asserts the P0 gate on the P3 tool surface (ADR-019).
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
    settings.HHT_VENDOR_CALLBACK_WINDOW = "one business day"
    settings.PHONE_HASH_PEPPER = "test-pepper-distinct"


def _tool_calls_payload():
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": "call_vendor_forged", "customer": {"number": "+15095551212"}},
            "toolCalls": [
                {
                    "id": "tc_1",
                    "function": {
                        "name": "notify_vendor_callback",
                        "arguments": {
                            "store": "yakima",
                            "reason": "delivery",
                            "summary": "delivery + manifest, no answer",
                        },
                    },
                }
            ],
        }
    }


@pytest.mark.django_db
def test_forged_vendor_tool_call_no_signature_rejected(client):
    """No signature → 401; NO VendorCallback, NO VoiceCall, NO email."""
    from django.core import mail

    from crm.models import VendorCallback
    from voice.models import VoiceCall

    raw = json.dumps(_tool_calls_payload()).encode()
    resp = client.post(WEBHOOK_URL, data=raw, content_type="application/json")
    assert resp.status_code == 401
    assert VendorCallback.objects.filter(vapi_call_id="call_vendor_forged").count() == 0
    assert VoiceCall.objects.filter(call_id="call_vendor_forged").count() == 0
    assert len(mail.outbox) == 0


@pytest.mark.django_db
def test_forged_vendor_tool_call_bad_signature_rejected(client):
    """A wrong HMAC signature → 401, no record, no email."""
    from crm.models import VendorCallback

    raw = json.dumps(_tool_calls_payload()).encode()
    resp = client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SIGNATURE": "deadbeef" * 8},
    )
    assert resp.status_code == 401
    assert VendorCallback.objects.filter(vapi_call_id="call_vendor_forged").count() == 0


@pytest.mark.django_db
def test_valid_signature_runs_vendor_handler(client):
    """A correct signature → 200, the VendorCallback is logged, the tool-result envelope returns."""
    from crm.models import VendorCallback

    raw = json.dumps(_tool_calls_payload()).encode()
    sig = signing.compute_signature(raw, SECRET)
    resp = client.post(
        WEBHOOK_URL, data=raw, content_type="application/json", **{"HTTP_X_VAPI_SIGNATURE": sig}
    )
    assert resp.status_code == 200
    body = resp.json()
    result = body["results"][0]["result"]
    assert result["logged"] is True
    assert result["callback_window"] == "one business day"
    assert VendorCallback.objects.filter(vapi_call_id="call_vendor_forged").count() == 1
