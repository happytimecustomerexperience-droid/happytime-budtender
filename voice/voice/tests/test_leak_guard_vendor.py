"""P3 — Leak-Guard on the vendor tool (13-P3 §7 F1; MANDATORY GATE).

No "cost"/"margin" substring in any notify_vendor_callback response (ADR-008). The vendor surface
carries no product fields, but the guard is uniform across every tool — a regression that ever
slipped a cost/margin field through must be caught here too. Routes through ``dispatch`` (the
central scrub) AND a direct ``assert_no_leak`` on the raw handler output.
"""

from __future__ import annotations

import pytest

from voice import guardrails
from voice.tools import dispatch as dispatch_tool


@pytest.fixture(autouse=True)
def _cfg(settings):
    settings.STAFF_ALERT_EMAIL = ""  # no email needed; the response is what we vet
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.HHT_DEFAULT_STORE = "yakima"
    settings.PHONE_HASH_PEPPER = "test-pepper-distinct"


def _call():
    return dispatch_tool(
        "notify_vendor_callback",
        {"store": "yakima", "reason": "wholesale_order", "summary": "wholesale order question"},
        {"call_id": "vapi-leak-1", "caller_number": "+15095559999"},
    )


@pytest.mark.django_db
def test_no_cost_or_margin_in_vendor_response():
    """F1: the dispatched result has no forbidden key/substring (assert_no_leak does not raise)."""
    result = _call()
    guardrails.assert_no_leak(result)  # raises LeakError on any cost/margin leak

    blob = str(result).lower()
    assert "cost" not in blob
    assert "margin" not in blob
