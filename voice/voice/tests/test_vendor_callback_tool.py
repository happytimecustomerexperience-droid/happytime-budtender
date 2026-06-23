"""P3 — notify_vendor_callback integration (13-P3 §7 C; §8.2). Vapi + email at the locmem layer.

Asserts (C1) a valid call writes an idempotent VendorCallback + sets VoiceCall.outcome /reason +
returns the §4.2 envelope with the config window; (C2) a re-delivered identical call returns the
SAME callback_id, no 2nd row, no 2nd email; (C3) the window is config-sourced; (C4) the staff alert
fires immediate. The email sink runs against the locmem backend (P2's plumbing) — no network.
"""

from __future__ import annotations

import pytest

from voice.tools import dispatch as dispatch_tool

CALL_ID = "vapi-call-vendor-1"
ARGS = {
    "store": "yakima",
    "reason": "delivery",
    "summary": "Driver from GreenLeaf has a delivery + manifest for receiving; no one answered.",
    "caller_name": "Marcus (GreenLeaf)",
}


@pytest.fixture(autouse=True)
def _cfg(settings):
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.HHT_DEFAULT_STORE = "yakima"
    settings.HHT_VENDOR_CALLBACK_WINDOW = "one business day"
    settings.PHONE_HASH_PEPPER = "test-pepper-distinct"


def _ctx():
    return {"call_id": CALL_ID, "store": "yakima", "caller_number": "+15095551212"}


@pytest.mark.django_db
def test_writes_callback_sets_outcome_and_returns_envelope():
    """C1: a valid call → VendorCallback row + VoiceCall(outcome=vendor_callback, reason=vendor) +
    the §4.2 envelope with the config window."""
    from django.core import mail

    from crm.models import VendorCallback
    from voice.models import Outcome, VoiceCall

    result = dispatch_tool("notify_vendor_callback", ARGS, _ctx())

    assert result["logged"] is True
    assert result["callback_id"]
    assert result["callback_window"] == "one business day"
    assert result["store"] == "yakima"
    assert result["reason"] == "delivery"
    assert result["alerted"] is True
    assert "one business day" in result["spoken"]

    cb = VendorCallback.objects.get(vapi_call_id=CALL_ID)
    assert cb.store == "yakima"
    assert cb.reason == "delivery"
    assert cb.caller_name == "Marcus (GreenLeaf)"
    assert cb.callback_window == "one business day"
    assert cb.alerted is True
    assert cb.caller_phone_hash and len(cb.caller_phone_hash) == 64

    vc = VoiceCall.objects.get(call_id=CALL_ID)
    assert vc.outcome == Outcome.VENDOR_CALLBACK
    assert vc.reason == "vendor"

    # C4: an immediate staff alert email fired.
    assert len(mail.outbox) == 1
    assert "URGENT" in mail.outbox[0].subject


@pytest.mark.django_db
def test_idempotent_redelivery_no_second_row_no_second_email():
    """C2: a re-delivered identical tool-call → SAME callback_id, 1 row, 1 email (alerted:false)."""
    from django.core import mail

    from crm.models import VendorCallback

    first = dispatch_tool("notify_vendor_callback", ARGS, _ctx())
    assert len(mail.outbox) == 1
    assert first["alerted"] is True

    second = dispatch_tool("notify_vendor_callback", ARGS, _ctx())
    assert second["callback_id"] == first["callback_id"]  # same row
    assert second["alerted"] is False  # the alert already went; not re-fired
    assert VendorCallback.objects.filter(vapi_call_id=CALL_ID).count() == 1
    assert len(mail.outbox) == 1  # no 2nd email


@pytest.mark.django_db
def test_window_is_config_sourced(settings):
    """C3: the spoken window equals HHT_VENDOR_CALLBACK_WINDOW (never LLM-originated)."""
    settings.HHT_VENDOR_CALLBACK_WINDOW = "the next business morning"
    result = dispatch_tool("notify_vendor_callback", ARGS, _ctx())
    assert result["callback_window"] == "the next business morning"
    assert "the next business morning" in result["spoken"]


@pytest.mark.django_db
def test_missing_reason_and_store_degrade_safe(settings):
    """§4.2 unknown/invalid args: missing store → default; missing reason → folded/other; no 500."""
    result = dispatch_tool(
        "notify_vendor_callback",
        {"summary": "dropping off a pallet for receiving"},
        {"call_id": "vapi-call-vendor-2", "caller_number": "+15095550000"},
    )
    assert result["logged"] is True
    assert result["store"] == "yakima"  # HHT_DEFAULT_STORE
    assert result["reason"] == "delivery"  # folded from the summary
