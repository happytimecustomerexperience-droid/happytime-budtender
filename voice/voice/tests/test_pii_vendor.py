"""P3 — PII discipline on the vendor path (13-P3 §7 D2; §8.1; ADR-006/019).

The raw caller number is absent from EVERY persisted field of VendorCallback + VoiceCall; only the
peppered ``caller_phone_hash`` is stored, and it equals ``crm.models.phone_hash(number)``.
``caller_name`` is the spoken name/company — never a phone number.
"""

from __future__ import annotations

import pytest

from voice.tools import dispatch as dispatch_tool

RAW_NUMBER = "+15095551212"
CALL_ID = "vapi-pii-1"


@pytest.fixture(autouse=True)
def _cfg(settings):
    settings.STAFF_ALERT_EMAIL = ""  # skip email; we only inspect persisted rows
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.HHT_DEFAULT_STORE = "yakima"
    settings.PHONE_HASH_PEPPER = "test-pepper-distinct"


@pytest.mark.django_db
def test_raw_number_never_persisted_only_hash():
    """D2: the raw number is in NO persisted field; the stored hash == phone_hash(number)."""
    from crm.models import VendorCallback, phone_hash
    from voice.models import VoiceCall

    dispatch_tool(
        "notify_vendor_callback",
        {
            "store": "yakima",
            "reason": "delivery",
            "summary": "delivery, no answer",
            "caller_name": "Marcus (GreenLeaf)",
        },
        {"call_id": CALL_ID, "store": "yakima", "caller_number": RAW_NUMBER},
    )

    cb = VendorCallback.objects.get(vapi_call_id=CALL_ID)
    vc = VoiceCall.objects.get(call_id=CALL_ID)

    # The peppered hash is stored and matches the canonical helper.
    assert cb.caller_phone_hash == phone_hash(RAW_NUMBER)
    assert vc.caller_phone_hash == phone_hash(RAW_NUMBER)

    # The raw number appears in NO persisted field of either row (a digit-string scan).
    digits = "5095551212"
    for value in (
        cb.store,
        cb.reason,
        cb.summary,
        cb.caller_name,
        cb.caller_phone_hash,
        cb.callback_window,
        vc.transcript,
        vc.reason,
        vc.caller_phone_hash,
    ):
        assert RAW_NUMBER not in (value or "")
        assert digits not in (value or "")
