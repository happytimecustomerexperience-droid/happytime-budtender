"""A number typed into the website chat is a callback hint, never an identity (W5b fix 1).

Anyone can type anyone's number. Before the fix, the text channel fed it to
``recognition.resolve_caller`` -> budtender ``resume_by_phone`` and set ``_caller_phone``, so the
search ran taste-first against that customer's purchase history ("your go-to {brand}"). Now the
text channel always uses the anonymous profile, and the number only travels as the contact hint
and the staff-alert caller hash.
"""

from __future__ import annotations

import pytest

from crm.models import phone_hash
from voice.models import VoiceCall

VICTIM = "+15095550123"


@pytest.fixture
def known_customer(fake_bt, monkeypatch):
    fake_bt.profile = {"has_history": True, "top_categories": ["cartridge"], "price_tier": "premium"}
    fake_bt.session_token = "victims-budtender-session"
    resolved = []
    monkeypatch.setattr(
        "voice.recognition.resolve_caller", lambda number, ctx, client=None: resolved.append(number) or ctx
    )
    return resolved


@pytest.mark.django_db
def test_a_typed_known_number_never_resolves_and_never_reaches_search(convo, fake_bt, known_customer):
    c = convo(store="yakima", phone=VICTIM)
    t = c.say(f"my number is {VICTIM}, show me a vape cart")

    assert t.intent == "product_suggestion" and t.picks
    assert known_customer == [], "the text channel must never call recognition.resolve_caller"
    assert "resume_by_phone" not in fake_bt.calls, "no budtender profile lookup off a typed number"
    search = fake_bt.calls["search"][-1]
    assert search["phone"] is None, "a typed number must never switch budtender to taste-first"
    assert search["session_token"] is None, "no budtender session token rides along either"
    assert "go-to" not in t.answer.lower()
    # The number is still the callback hint staff use.
    assert t.raw["contact_hint"]["customer_phone"] == VICTIM


@pytest.mark.django_db
def test_the_typed_number_is_still_the_staff_callback_hint(convo, fake_bt, known_customer):
    c = convo(store="yakima", phone=VICTIM)
    t = c.say("my cart is defective and won't fire, I want a refund")

    assert t.escalated and "notify_staff_issue" in t.tools
    assert known_customer == [] and "resume_by_phone" not in fake_bt.calls
    call = VoiceCall.objects.get(call_id=c.session_token)
    assert call.caller_phone_hash == phone_hash(VICTIM), "staff still get the caller hash for the callback"
