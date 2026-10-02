"""W5b fix 7 — nothing may claim staff were told unless the staff alert actually went out.

``notify_vendor_callback`` always said "I've let the {store} team know and someone will call you
back", and the chat restock reply always said "I've passed this to the store team" — including when
the email was switched off, failed, capped, or there was no number to call back.
"""

from __future__ import annotations

import pytest

from voice import chat
from voice.safety_copy import FOLLOWUP_NOT_CONFIRMED
from voice.tools import dispatch as dispatch_tool

ARGS = {"store": "yakima", "reason": "delivery", "summary": "Driver with a manifest; no one answered."}


@pytest.fixture
def email_on(settings):
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.HHT_VENDOR_CALLBACK_WINDOW = "one business day"


@pytest.mark.django_db
def test_vendor_callback_promised_only_when_alerted_and_reachable(email_on):
    told = dispatch_tool("notify_vendor_callback", ARGS, {"call_id": "vapi-1", "caller_number": "+15095551212"})
    assert told["alerted"] is True
    assert "let the Yakima team know" in told["spoken"] and "one business day" in told["spoken"]

    again = dispatch_tool("notify_vendor_callback", ARGS, {"call_id": "vapi-1", "caller_number": "+15095551212"})
    assert again["alerted"] is False, "the re-delivery fires no second alert"
    assert again["spoken"] == told["spoken"], "...but the team WAS told the first time, so the promise stands"

    no_number = dispatch_tool("notify_vendor_callback", ARGS, {"call_id": "vapi-2", "caller_number": ""})
    assert no_number["alerted"] is True
    assert no_number["spoken"] == FOLLOWUP_NOT_CONFIRMED, "no number, so no callback can be promised"


@pytest.mark.django_db
def test_vendor_callback_with_no_alert_or_no_record_promises_nothing(settings):
    settings.STAFF_ALERT_EMAIL = ""  # email not configured: the sink is skipped
    out = dispatch_tool("notify_vendor_callback", ARGS, {"call_id": "vapi-3", "caller_number": "+15095551212"})
    assert out["alerted"] is False and out["logged"] is True
    assert out["spoken"] == FOLLOWUP_NOT_CONFIRMED

    unrecorded = dispatch_tool("notify_vendor_callback", ARGS, {"caller_number": "+15095551212"})
    assert unrecorded["logged"] is False and unrecorded["callback_id"] is None
    assert unrecorded["spoken"] == FOLLOWUP_NOT_CONFIRMED


@pytest.mark.django_db
def test_escalation_confirmation_only_when_alerted(email_on, settings):
    sent = dispatch_tool("notify_staff_issue", {"issue_type": "dispute", "summary": "wrong item"},
                         {"call_id": "vapi-4", "store": "yakima"})
    assert sent["alerted"] is True and "sent all of it straight to our Yakima team" in sent["spoken"]

    settings.STAFF_ALERT_EMAIL = ""
    unsent = dispatch_tool("notify_staff_issue", {"issue_type": "dispute", "summary": "wrong item"},
                           {"call_id": "vapi-5", "store": "yakima"})
    assert unsent["alerted"] is False and unsent["spoken"] == FOLLOWUP_NOT_CONFIRMED


@pytest.mark.parametrize("alerted", [True, False])
def test_restock_reply_follows_the_tool_result(monkeypatch, alerted):
    def fake_dispatch(tool, args, ctx):
        if tool == "faq_lookup":
            return {"grounded": False, "fallback": "no match"}
        return {"logged": True, "alerted": alerted, "spoken": "x"}

    monkeypatch.setattr(chat, "dispatch", fake_dispatch)
    out = chat.answer_text_chat(
        {"message": "can you text me when the blue dream is back in stock", "phone": "5095551212", "store": "yakima"}
    )
    assert "notify_staff_issue" in [t["tool"] for t in out["tool_results"]]
    if alerted:
        assert "I've passed this to the store team" in out["answer"]
    else:
        assert "passed this to the store team" not in out["answer"]
        assert out["answer"].endswith(FOLLOWUP_NOT_CONFIRMED)


@pytest.mark.parametrize("alerted", [True, False])
def test_opt_out_reply_follows_the_tool_result(monkeypatch, alerted):
    def fake_dispatch(tool, args, ctx):
        if tool == "faq_lookup":
            return {"grounded": False, "fallback": "no match"}
        return {"logged": True, "alerted": alerted, "spoken": "x"}

    monkeypatch.setattr(chat, "dispatch", fake_dispatch)
    out = chat.answer_text_chat({"message": "please stop texting me", "phone": "5095551212", "store": "yakima"})
    assert ("passed your request to the store team" in out["answer"]) is alerted
