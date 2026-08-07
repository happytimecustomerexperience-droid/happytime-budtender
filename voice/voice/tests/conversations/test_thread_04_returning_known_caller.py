"""Thread 04 — Marisol, a regular whose number budtender knows: recognition, taste-first search, and the profile-category fallback that answers a bare "what do you recommend"."""

from __future__ import annotations

import pytest

PHONE = "+15095550123"


@pytest.mark.django_db
def test_returning_caller_recognised_and_ranked_by_taste(convo, fake_bt):
    """Five turns from a known number: hours → bare recommend → budget → new category → policy."""
    fake_bt.profile = {
        "has_history": True,
        "top_categories": ["cartridge", "flower"],
        "price_tier": "mid",
    }
    c = convo(store="yakima", phone=PHONE)

    # 1. She opens with a plain FAQ question. Recognition still has to run on this turn —
    #    it is not deferred until she asks for a product.
    t = c.say("hey it's Marisol again, are you open till nine tonight")
    assert t.intent == "hours_location"
    assert t.grounded, "hours must come from the KB, never invented"
    assert t.sources
    assert t.tools == ["faq_lookup"], "an hours question must not turn into a product pitch"
    lookup = fake_bt.calls["resume_by_phone"]
    assert len(lookup) == 1, "the caller is recognised before anything else happens"
    assert lookup[0]["phone"] == PHONE, "the raw E.164 is what budtender resolves the profile by"
    assert lookup[0]["location"] == "yakima"
    assert "search" not in fake_bt.calls, "no inventory call on an hours turn"

    # 2. The bare ask. She names no category — the profile's top category has to supply it.
    t = c.say("cool. honestly just tell me what you'd recommend today")
    assert t.intent == "product_suggestion", "a known caller's bare 'recommend' must reach products"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge", "category came from profile top_categories, not the text"
    assert t.picks, "profile category with no picks means the fallback never reached search"
    assert t.next_action == "show_products"
    search = fake_bt.calls["search"][-1]
    assert search["phone"] == PHONE, "known caller's phone must reach search — it is the taste-first switch"
    assert search["session_token"] == "sess-known-1", "the resumed session rides along too"
    assert search["location"] == "yakima"
    assert search["slots"]["category"] == "cartridge"
    assert t.pick_names == [
        "Avitas GSC 0.5g Cart",
        "Jetty Blue Dream 1g Cart",
        "Drum Roll Granddaddy 1g",
    ]

    # 3. She reacts to what she just heard — still no category word, still a cartridge search.
    t = c.say("those are pricier than I remember, anything under $25")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge", "the profile category has to survive a follow-up turn"
    assert args["price_max"] == 25.0
    search = fake_bt.calls["search"][-1]
    assert search["slots"]["price_max"] == 25.0, "the budget must reach the client, not just the args"
    assert search["phone"] == PHONE
    assert t.pick_names == ["Avitas GSC 0.5g Cart"]
    for pick in t.picks:
        assert pick["price_otd"] > 22.0, "the spoken price is the out-the-door uplift, not the shelf price"
        assert "cost" not in pick and "margin" not in pick

    # 4. A named category beats the profile — and the derived effect is mapped into budtender's
    #    vocabulary instead of being dropped (fixed 2026-08-07).
    t = c.say("my sister is coming over, got gummies that help with sleep")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "edible", "an explicit category must override the profile's cartridge"
    assert args["effect_desired"] == "relaxed", "_EFFECT_TO_BUDTENDER maps sleep -> relaxed pre-dispatch"
    search = fake_bt.calls["search"][-1]
    assert search["slots"]["category"] == "edible"
    # "relaxed" is in suggest_products' JSON-Schema enum {relaxed, uplifted, middle}, so it
    # survives _sanitize_args and actually reaches the ranker now.
    assert search["slots"]["effect_desired"] == "relaxed", "the mapped effect now reaches budtender"
    assert search["phone"] == PHONE, "taste-first ranking still applies on the new category"
    assert "Wyld Raspberry Gummies 10mg" in t.pick_names

    # 5. A policy question. The profile fallback must NOT fire here — a returning caller asking
    #    about returns gets the KB, not a cartridge.
    t = c.say("last thing, remind me what the return policy is")
    assert t.intent == "return_policy"
    assert t.tools == ["faq_lookup"], "a sourced-policy question must never route to inventory"
    assert t.grounded and t.sources, "policy answers are grounded or they are not given"
    assert not t.escalated

    assert len(c.turns) == 5
    assert len(fake_bt.calls["search"]) == 3, "turns 2-4 searched; turns 1 and 5 did not"
    assert len(fake_bt.calls["resume_by_phone"]) == 5, "text chat re-resolves the caller every turn"
    assert all(call["phone"] == PHONE for call in fake_bt.calls["search"])


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
    # FINDING (see report): the same words that steered the known caller into inventory land the
    # anonymous one on a grounded specials blurb labelled `greeting_other` — the label and the
    # answer disagree. Asserted as it actually behaves.
    assert (t.intent, t.next_action, t.grounded) == ("greeting_other", "answer", True)
    assert "deals" in t.answer.lower()

    t = c.say("okay, then show me a cartridge")
    assert t.intent == "product_suggestion", "an explicit category works without recognition"
    search = fake_bt.calls["search"][-1]
    assert search["phone"] is None, "anonymous search stays margin-first — no identity forwarded"
    assert search["session_token"] is None
    assert t.picks


@pytest.mark.django_db
def test_profile_categories_are_normalised_and_junk_entries_skipped(convo, fake_bt):
    """A profile whose top category is a vendor alias ('VAPES') behind an unrecognised one."""
    fake_bt.profile = {
        "has_history": True,
        "top_categories": ["accessories", "VAPES"],
        "price_tier": "value",
    }
    c = convo(store="pullman", phone=PHONE)

    # FINDING (see report): a bare hello from a known caller is answered with a product pitch —
    # any non-FAQ utterance inherits the profile category. Asserted as it actually behaves.
    t = c.say("hey, it's Marisol")
    assert t.intent == "product_suggestion"
    assert t.args("suggest_products")["category"] == "cartridge", (
        "'accessories' is not a sellable category and is skipped; 'VAPES' normalises to cartridge"
    )
    assert fake_bt.calls["resume_by_phone"][0]["location"] == "pullman"

    t = c.say("what's good today")
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["store"] == "pullman", "the store follows the caller, not the default"
    search = fake_bt.calls["search"][-1]
    assert search["location"] == "pullman"
    assert search["phone"] == PHONE
    assert t.picks
