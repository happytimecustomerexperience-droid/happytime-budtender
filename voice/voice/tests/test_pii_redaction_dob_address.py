"""redact_pii extended to mask DOB-shaped values and street addresses spoken on a cannabis-
retail call (age verification, delivery/callback address) — neither shape is a contiguous
phone-like digit run, so the existing ``_PHONE_RE`` let both through to stored transcripts/turns
in the clear. This is a privacy/compliance gap for a licensed WA retailer.

Two halves:
  1. Unit-level: the new shapes ARE masked, and the existing safe vocabulary (prices, weights/
     doses, legal citations, hours, percentages/promo dates, product names with numbers) is NOT
     touched — a greedy date/number matcher would mangle the whole call log.
  2. End-to-end: post a signed end-of-call-report whose transcript carries a DOB and an address
     and assert the stored VoiceCall.transcript / VoiceTurn.text carry neither (persistence path,
     not just the regex in isolation).

KNOWN TRADEOFF: the store's own address (e.g. "1315 N 1st St") spoken by the agent will also be
masked in stored transcripts under this rule. Accepted — the redactor can't tell caller-address
from store-address, and over-redacting a non-secret beats under-redacting a caller's home address.
"""

from __future__ import annotations

import json

import pytest

from voice import guardrails, signing

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


# ── unit: new shapes ARE masked ─────────────────────────────────────────────────
@pytest.mark.parametrize("dob", ["03/04/1990", "3-4-1990", "1990/03/04"])
def test_redact_pii_masks_dob_shapes(dob):
    out = guardrails.redact_pii({"note": f"caller says her birthday is {dob}, checking id"})
    assert dob not in out["note"]
    assert "[redacted]" in out["note"]


@pytest.mark.parametrize("addr", ["123 Main St", "1315 N 1st St, Yakima"])
def test_redact_pii_masks_street_address(addr):
    out = guardrails.redact_pii({"note": f"deliver to {addr} please"})
    assert addr not in out["note"]
    assert "[redacted]" in out["note"]


# ── unit: must-NOT-redact vocabulary (collateral-damage guard) ─────────────────
_MUST_SURVIVE = [
    "$40",
    "40 dollars",
    "30 dollars and 50 cents",
    "8 dollars",
    "3.5g",
    "1g",
    "0.5g",
    "10mg",
    "5mg",
    "28g",
    "an eighth",
    "WAC 314-55-079",
    "RCW 69.50.535",
    "8 AM-11:30 PM daily",
    "9 AM-10 PM",
    "30% off",
    "July 1 through July 31",
    "Gorilla Glue #4 3.5g",
    "Blue Dream 1g Cart",
]


@pytest.mark.parametrize("phrase", _MUST_SURVIVE)
def test_redact_pii_does_not_touch_safe_vocabulary(phrase):
    out = guardrails.redact_pii({"note": f"context around {phrase} more context"})
    assert phrase in out["note"], f"{phrase!r} was mangled: {out['note']!r}"


def test_redact_pii_full_sentence_survives_intact():
    """A realistic FAQ-ish sentence with prices/weights/citations/hours/promo all present at once
    must come back byte-identical — a compound sentence is where a greedy matcher would bite."""
    sentence = (
        "Blue Dream 1g Cart is $40, Gorilla Glue #4 3.5g is 30 dollars and 50 cents, an eighth "
        "runs 8 dollars off during the 30% off promo July 1 through July 31, per WAC 314-55-079 "
        "and RCW 69.50.535, store hours are 8 AM-11:30 PM daily."
    )
    out = guardrails.redact_pii({"note": sentence})
    assert out["note"] == sentence


# ── end-to-end: signed EOCR persistence path masks DOB + address, not the safe vocabulary ──
def _eocr(call_id):
    return {
        "message": {
            "type": "end-of-call-report",
            "call": {"id": call_id, "customer": {"number": "+15095551212"}, "assistantId": "asst_1"},
            "endedReason": "customer-ended-call",
            "durationSeconds": 40,
            "transcript": (
                "User: I was born 03/04/1990, please deliver to 123 Main St. "
                "AI: Sure, Blue Dream 1g Cart is $40 and store hours are 8 AM-11:30 PM daily."
            ),
            "messages": [
                {"role": "user", "message": "I was born 03/04/1990, please deliver to 123 Main St"},
                {"role": "bot", "message": "Blue Dream 1g Cart is $40, hours are 8 AM-11:30 PM daily"},
            ],
        }
    }


@pytest.mark.django_db
def test_eocr_persistence_masks_dob_and_address_but_not_safe_vocabulary(client):
    from voice.models import VoiceCall, VoiceTurn

    resp = _post(client, _eocr("call_pii_dob"))
    assert resp.status_code == 200

    vc = VoiceCall.objects.get(call_id="call_pii_dob")
    assert "03/04/1990" not in vc.transcript
    assert "123 Main St" not in vc.transcript
    assert "$40" in vc.transcript
    assert "8 AM-11:30 PM daily" in vc.transcript

    turns = list(VoiceTurn.objects.filter(call=vc).order_by("seq"))
    combined = " ".join(t.text for t in turns)
    assert "03/04/1990" not in combined
    assert "123 Main St" not in combined
    assert "$40" in combined
    assert "8 AM-11:30 PM daily" in combined
