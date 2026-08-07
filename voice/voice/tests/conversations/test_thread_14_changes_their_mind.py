"""Thread 14 — Dana starts on flower, switches to edibles mid-call, then asks to add one small thing.

Proves the router re-derives every product slot from the CURRENT turn (a category switch leaks no
prior subcategory/size), pins where that statelessness costs the caller, and shows the gated upsell
surface is never reached from a conversation.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_dana_switches_flower_to_edibles_then_adds_something_small(convo, fake_bt):
    c = convo(store="yakima")

    # 1) Opens on flower, with a size and a subcategory in one breath.
    t = c.say("hey there, I'm after an eighth of indica flower for tonight")
    assert t.intent == "product_suggestion"
    opener = t.args("suggest_products")
    assert opener["category"] == "flower"
    assert opener["subcategory"] == "indica"
    assert opener["size"] == "3.5g"
    assert t.pick_names == ["Blueberry OG 3.5g"]
    assert t.next_action == "show_products"
    first_sku = t.picks[0]["sku"]

    # 2) Changes her mind out loud, in the words a caller actually uses. FIXED 2026-08-07: the
    #    plural "edibles" now matches the plural-tolerant ``_CATEGORY_RE`` (fix 2), so the switch
    #    lands on the FIRST try — and it must still carry NO subcategory and NO size from turn 1,
    #    or she gets "indica 3.5g edibles" and hears nothing.
    t = c.say("actually hold on, my wife can't smoke - do you have edibles instead")
    assert t.intent == "product_suggestion"
    switched = t.args("suggest_products")
    assert switched["category"] == "edible", "the spoken switch re-derives the category"
    assert "subcategory" not in switched, f"turn-1 'indica' leaked into the edible ask: {switched}"
    assert "size" not in switched, f"turn-1 '3.5g' leaked into the edible ask: {switched}"
    sent = fake_bt.calls["search"][-1]["slots"]
    assert sent["category"] == "edible"
    assert "subcategory" not in sent and "size" not in sent
    assert t.picks and all(p["sku"].startswith("ED-") for p in t.picks), t.pick_names

    # 3) She repeats herself anyway, the way callers do even when they were already understood.
    #    The router re-derives fresh every turn — same correct, non-leaking result the second time.
    t = c.say("sorry - gummies I mean, what have you got")
    assert t.intent == "product_suggestion"
    switched = t.args("suggest_products")
    assert switched["category"] == "edible", "the spoken switch has to re-derive the category"
    assert "subcategory" not in switched, f"turn-1 'indica' leaked into the edible ask: {switched}"
    assert "size" not in switched, f"turn-1 '3.5g' leaked into the edible ask: {switched}"
    assert "effect_desired" not in switched
    sent = fake_bt.calls["search"][-1]["slots"]
    assert sent["category"] == "edible"
    assert "subcategory" not in sent and "size" not in sent
    assert t.picks and all(p["sku"].startswith("ED-") for p in t.picks), t.pick_names

    # 4) Narrows inside the NEW category. The size must come from this turn, not the flower turn.
    t = c.say("she's never had one - are the 10mg gummies okay for a beginner")
    narrowed = t.args("suggest_products")
    assert narrowed["category"] == "edible"
    assert narrowed["size"] == "10mg", "size is re-derived per turn, so 10mg must land here"
    assert t.pick_names == ["Wyld Raspberry Gummies 10mg"]

    # 5) The small add-on. FIXED 2026-08-07: "pre-roll" is now in the suggest_products category
    #    enum (constants.py) and mapped in budtender's CATEGORY_BY_SLOTKEY (fix 4), so the
    #    router's correct category reaches the catalog instead of being dropped at the schema
    #    wall and bouncing off the handler's own required-field check.
    searches_before = len(fake_bt.calls["search"])
    assert any(r["category"] == "pre-roll" and r["price"] <= 10 for r in fake_bt.catalog), (
        "the catalog does stock a cheap single pre-roll"
    )
    t = c.say("perfect, can I add a cheap single pre-roll to that under $10")
    addon = t.args("suggest_products")
    assert addon["category"] == "pre-roll", "the router does recognise a pre-roll"
    assert addon["price_max"] == 10.0
    assert t.result("suggest_products").get("error") is None
    assert t.pick_names == ["Single Pre-roll 1g"], "the cheap single pre-roll the catalog stocks"
    assert t.grounded is True
    assert t.next_action == "show_products"
    assert len(fake_bt.calls["search"]) == searches_before + 1, (
        "the pre-roll ask now reaches budtender"
    )

    # 6) She refers back — but SAYS the words again, so the stateless router finds it a second time.
    t = c.say("okay and remind me what that indica eighth was again")
    reheard = t.args("suggest_products")
    assert reheard["category"] == "flower"
    assert reheard["subcategory"] == "indica"
    assert reheard["size"] == "3.5g"
    assert t.picks[0]["sku"] == first_sku

    # 7) The same back-reference WITHOUT restating it: nothing carries, so the product route is gone.
    t = c.say("and how much was the first one I asked about")
    assert "suggest_products" not in t.tools, (
        "FINDING: a pronoun-only reference loses the product route entirely"
    )
    assert t.tools == ["faq_lookup"]
    assert t.picks == []
    assert t.intent == "greeting_other"
    assert t.next_action != "show_products"
    # ...and, like turn 2, the miss is dressed up as a confident grounded answer about something
    # else entirely (the KB keyword path — semantic search is off in this offline suite).
    assert t.grounded is True
    assert t.sources[0]["title"] == 'Is Happy Times the same as Happy Time? What about "happytime"?'

    assert len(c.turns) == 7
    assert "pair_for_sku" not in fake_bt.calls


@pytest.mark.django_db
def test_client_supplied_slots_pin_her_to_the_category_she_left(convo, fake_bt):
    """Same switch, but the caller's client keeps the old slots — they beat what she just said."""
    c = convo(
        store="yakima",
        slots={"category": "flower", "subcategory": "indica", "size": "3.5g", "price_max": 45.0},
    )

    t = c.say("actually can we switch to edibles instead, something around 10mg")
    pinned = t.args("suggest_products")
    assert pinned["category"] == "flower", (
        "FINDING: caller-supplied slots win over the spoken switch — she asked for edibles"
    )
    assert pinned["size"] == "3.5g", "the 10mg she just said is ignored while a size slot is set"
    assert t.picks and all(p["sku"].startswith("FL-") for p in t.picks), t.pick_names

    t = c.say("no I mean edibles, gummies for my wife")
    assert t.args("suggest_products")["category"] == "flower"
    assert all(p["sku"].startswith("FL-") for p in t.picks), t.pick_names

    assert [call["slots"]["category"] for call in fake_bt.calls["search"]] == ["flower", "flower"]


@pytest.mark.django_db
def test_the_gated_upsell_never_reaches_the_conversation(convo, fake_bt):
    """A strong pairing sits ready on the anchor she just picked, and the call never asks for it."""
    from voice.tools import dispatch
    from voice.tools.suggest import PAIR_STRENGTH_GATE

    assert PAIR_STRENGTH_GATE == 0.40
    drink = next(r for r in fake_bt.catalog if r["sku"] == "ED-CQ-5")
    fake_bt.pairing = {
        "pairing": dict(drink),
        "strength": 0.72,  # comfortably above the gate
        "reason_text": "Folks who take the gummies grab the sparkling drink too.",
    }

    c = convo(store="yakima")
    t = c.say("I'll grab the raspberry gummies then")
    assert t.intent == "product_suggestion"
    assert t.picks
    assert "pair_upsell" not in t.tools

    t = c.say("anything small you'd throw in with those?")
    assert t.tools == ["faq_lookup"], "the natural upsell moment routes nowhere near pair_upsell"
    assert t.picks == []
    assert drink["name"] not in t.answer
    assert "pair_for_sku" not in fake_bt.calls, (
        "FINDING: answer_text_chat never dispatches pair_upsell, so the gate is unreachable by chat"
    )

    # The tool itself is healthy — dispatched directly it honours the gate in both directions.
    ctx = {"store": "yakima", "session_token": "convo-test"}
    above = dispatch("pair_upsell", {"store": "yakima", "anchor_sku": "ED-WYLD-10"}, ctx)
    assert above["offer"] is True
    assert above["pair"]["sku"] == "ED-CQ-5"
    assert above["pair"]["price_otd"] > 0
    assert "cost" not in above["pair"] and "margin" not in above["pair"]
    assert above["strength"] >= PAIR_STRENGTH_GATE

    fake_bt.pairing = {"pairing": dict(drink), "strength": 0.31}  # below the gate
    below = dispatch("pair_upsell", {"store": "yakima", "anchor_sku": "ED-WYLD-10"}, ctx)
    assert below == {"offer": False}
    assert len(fake_bt.calls["pair_for_sku"]) == 2
