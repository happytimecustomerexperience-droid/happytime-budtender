"""Thread 04 — Marisol, a regular whose number budtender knows, types it into the website chat.

UPDATED (W5b): a typed number is a callback hint, never an identity — anyone can type anyone's
number. So the text channel never looks it up: no ``resume_by_phone``, no phone or budtender session
token on the search, no profile-category fallback. She is served exactly like an anonymous caller,
and the number only rides along as the contact hint staff call back.
"""

from __future__ import annotations

import pytest

PHONE = "+15095550123"


@pytest.mark.django_db
def test_returning_caller_typed_number_is_never_an_identity(convo, fake_bt):
    """Five turns from a known number: hours → recommend → budget → new category → policy."""
    fake_bt.profile = {
        "has_history": True,
        "top_categories": ["cartridge", "flower"],
        "price_tier": "mid",
    }
    c = convo(store="yakima", phone=PHONE)

    # 1. A plain FAQ question. No profile lookup happens on this turn or any other.
    t = c.say("hey it's Marisol again, are you open till nine tonight")
    assert t.intent == "hours_location"
    assert t.grounded, "hours must come from the KB, never invented"
    assert t.sources
    assert t.tools == ["faq_lookup"], "an hours question must not turn into a product pitch"
    assert "resume_by_phone" not in fake_bt.calls, "a typed number is never looked up"
    assert "search" not in fake_bt.calls, "no inventory call on an hours turn"
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": PHONE}

    # 2. A bare ask. No profile is read, so there is no category to fall back on.
    t = c.say("cool. honestly just tell me what you'd recommend today")
    assert t.intent != "product_suggestion", "the profile on file must never supply a category"
    assert "search" not in fake_bt.calls

    # 3. She names the category herself — an ordinary anonymous, margin-first search.
    t = c.say("show me a cartridge under $25")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["price_max"] == 25.0
    search = fake_bt.calls["search"][-1]
    assert search["slots"]["price_max"] == 25.0, "the budget must reach the client, not just the args"
    assert search["phone"] is None, "a typed number must never switch budtender to taste-first"
    assert search["session_token"] is None, "no budtender session token rides along either"
    assert search["location"] == "yakima"
    assert t.pick_names == ["Avitas GSC 0.5g Cart"]
    for pick in t.picks:
        assert pick["price_otd"] == 22.0, "the spoken price is the menu price — tax-inclusive Dutchie account"
        assert "cost" not in pick and "margin" not in pick

    # 4. A named category, and the derived effect is mapped into budtender's vocabulary.
    t = c.say("my sister is coming over, got gummies that help with sleep")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "edible"
    assert args["effect_desired"] == "relaxed", "_EFFECT_TO_BUDTENDER maps sleep -> relaxed pre-dispatch"
    search = fake_bt.calls["search"][-1]
    assert search["slots"]["category"] == "edible"
    assert search["slots"]["effect_desired"] == "relaxed", "the mapped effect now reaches budtender"
    assert search["phone"] is None
    assert "Wyld Raspberry Gummies 10mg" in t.pick_names

    # 5. A policy question gets the KB, not a product.
    t = c.say("last thing, remind me what the return policy is")
    assert t.intent == "return_policy"
    assert t.tools == ["faq_lookup"], "a sourced-policy question must never route to inventory"
    assert t.grounded and t.sources, "policy answers are grounded or they are not given"
    assert not t.escalated

    assert len(c.turns) == 5
    assert len(fake_bt.calls["search"]) == 2, "turns 3-4 searched; turns 1, 2 and 5 did not"
    assert "resume_by_phone" not in fake_bt.calls
    assert all(call["phone"] is None for call in fake_bt.calls["search"])


@pytest.mark.django_db
def test_same_questions_without_the_number_get_no_recognition(convo, fake_bt):
    """The control: identical profile on file, but the caller withholds their number."""
    fake_bt.profile = {
        "has_history": True,
        "top_categories": ["cartridge", "flower"],
        "price_tier": "mid",
    }
    c = convo(store="yakima", phone="")

    t = c.say("hi there, are you open till nine tonight")
    assert t.intent == "hours_location"
    assert t.grounded
    assert "resume_by_phone" not in fake_bt.calls, "no number, no profile lookup"

    t = c.say("just tell me what you'd recommend today")
    assert t.intent != "product_suggestion", "with no profile there is no category to fall back on"
    assert t.tools == ["faq_lookup"]
    assert "search" not in fake_bt.calls, "an anonymous bare 'recommend' never reaches inventory"
    # FIXED 2026-09-17: this used to land on a grounded specials blurb labelled `greeting_other`
    # — a label and an answer that disagreed, and a confident answer to a question that was not
    # asked. With no profile and no category there is genuinely nothing to recommend from, so the
    # honest shape is the decline: ungrounded, hand to a person.
    assert (t.intent, t.next_action, t.grounded) == ("greeting_other", "ask_staff", False)
    assert "deals" not in t.answer.lower()

    t = c.say("okay, then show me a cartridge")
    assert t.intent == "product_suggestion", "an explicit category works without recognition"
    search = fake_bt.calls["search"][-1]
    assert search["phone"] is None, "anonymous search stays margin-first — no identity forwarded"
    assert search["session_token"] is None
    assert t.picks


@pytest.mark.django_db
def test_a_known_callers_hello_is_no_longer_a_product_pitch(convo, fake_bt):
    """Was a FINDING: a bare hello from a known caller inherited the profile's top category and
    was answered with a product pitch. With no profile read off a typed number, it is a hello."""
    fake_bt.profile = {
        "has_history": True,
        "top_categories": ["accessories", "VAPES"],
        "price_tier": "value",
    }
    c = convo(store="pullman", phone=PHONE)

    t = c.say("hey, it's Marisol")
    assert t.intent != "product_suggestion"
    assert "suggest_products" not in t.tools
    assert "resume_by_phone" not in fake_bt.calls and "search" not in fake_bt.calls
