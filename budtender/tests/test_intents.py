"""Conversation-intent classifier for the website chat. Trusts the voice router's
label when present; otherwise classifies the message itself so EVERY turn is
classified even when the voice brain is unreachable (local/fallback)."""
from __future__ import annotations

import pytest

from budtender.intents import INTENTS, classify_intent


def test_trusts_voice_response_intent():
    assert classify_intent("anything", {"intent": "specials"}) == "specials"


def test_ignores_invalid_voice_intent_and_falls_back_to_message():
    assert classify_intent("what is your return policy", {"intent": "bogus"}) == "return_policy"


@pytest.mark.parametrize("message,expected", [
    ("what is your return policy", "return_policy"),
    ("do you accept returns or exchanges", "return_policy"),
    # Bare "refund" is a dispute trigger (routes to staff) — matches the voice router.
    ("can I get a refund", "conflict_resolution"),
    ("any deals or specials today", "specials"),
    ("is there a sale on carts", "specials"),
    ("what time do you open", "hours_location"),
    ("what's your address and phone", "hours_location"),
    ("show me some edibles under $20", "product_suggestion"),
    ("looking for an indica flower eighth", "product_suggestion"),
    ("my cart is broken and I want a refund", "conflict_resolution"),
    ("this is a scam, I am furious", "conflict_resolution"),
    ("i got the wrong item", "conflict_resolution"),
    ("do you take debit cards", "general_faq"),
    ("hi there", "greeting_other"),
    ("", "greeting_other"),
    # regressions found by the 100-conversation corpus:
    ("any pre-rolls that are good for relaxing after work", "product_suggestion"),  # plural pre-rolls
    ("i want something with high thc like 25%+", "product_suggestion"),             # thc as a product signal
    ("do you have any coupons or promos rn", "specials"),                           # plural coupons/promos
    ("where exactly are you located near the mall", "hours_location"),              # 'located'
    ("do you have loyalty points or rewards", "general_faq"),                       # faq beats weak 'do you have'
    ("do you have curbside pickup available", "general_faq"),                       # faq beats weak 'do you have'
    ("can i speak to a real person about a problem with my order", "conflict_resolution"),  # human escalation
])
def test_classifies_message_without_voice_response(message, expected):
    assert classify_intent(message, None) == expected


def test_all_outputs_are_in_the_taxonomy():
    for msg in ["hi", "refund please", "specials", "edibles", "hours"]:
        assert classify_intent(msg, None) in INTENTS
