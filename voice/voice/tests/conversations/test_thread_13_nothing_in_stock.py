"""Thread 13 — the caller whose whole list is sold out: the agent says so plainly, then recovers.

Proves the honest-empty path (no invented substitutes, ``ask_staff``, ``grounded`` false) survives a
run of misses, that a miss is never a routing failure, and that the very next turn suggests properly
once stock comes back.
"""

from __future__ import annotations

import pytest

HONEST_EMPTY_SUMMARY = "I'm not finding that in stock right now."


def assert_honest_empty(turn, fake_bt):
    """Every miss must look the same: no picks, no invented product, staff handoff, not grounded."""
    assert turn.intent == "product_suggestion"
    assert turn.tools == ["faq_lookup", "suggest_products"]
    assert turn.picks == [], f"budtender returned nothing — picks must stay empty, got {turn.pick_names}"
    assert turn.result("suggest_products")["spoken_summary"] == HONEST_EMPTY_SUMMARY
    assert "can't find any matching items in stock" in turn.answer
    assert turn.grounded is False, "nothing was found, so nothing is grounded"
    # Documented oddity: the ungrounded miss still ships a source card for the shelf it checked.
    assert turn.sources == [{"kind": "tool", "title": "Live budtender inventory"}]
    assert turn.next_action == "ask_staff"
    assert turn.escalated is False, "a sold-out shelf is not a dispute"
    for row in fake_bt.catalog:
        assert row["name"] not in turn.answer, f"agent invented a substitute: {row['name']}"
    assert "$" not in turn.answer, "no price may appear when nothing was found"


@pytest.mark.django_db
def test_everything_is_sold_out_then_the_shelf_comes_back(convo, fake_bt):
    """One continuous call: three misses (two dead searches, one over-tight budget), then recovery."""
    c = convo(store="yakima")

    # 1 — budtender comes back empty for everything.
    fake_bt.fail_search = True

    t = c.say("hi there, do you have any live rosin left today")
    assert t.args("suggest_products")["category"] == "concentrate"
    assert_honest_empty(t, fake_bt)

    # 2 — a different category, same dead shelf. The miss is NOT a routing miss: the slots the
    #     router derived are right and they reached the client.
    t = c.say("hmm ok. what about a strong indica cart then")
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["subcategory"] == "indica"
    assert_honest_empty(t, fake_bt)
    assert fake_bt.calls["search"][-1]["slots"]["category"] == "cartridge"
    assert fake_bt.calls["search"][-1]["slots"]["subcategory"] == "indica"

    # 3 — stock is fine now; it's the caller's budget that leaves the shelf empty. Same honest miss.
    fake_bt.fail_search = False
    t = c.say("any chance you've got an indica eighth for under 20 bucks")
    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert args["subcategory"] == "indica"
    assert args["size"] == "3.5g"
    assert args["price_max"] == 20.0
    assert fake_bt.calls["search"][-1]["slots"]["price_max"] == 20.0, "the budget must reach the client"
    assert_honest_empty(t, fake_bt)

    # 4 — drop the budget and the same category finally lands. Recovery on the next turn.
    t = c.say("alright, forget the budget then — what indica flower do you have")
    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert args["subcategory"] == "indica"
    assert "price_max" not in args, "the old budget must not stick to a later turn"
    assert t.pick_names == ["Blueberry OG 3.5g"], "the one indica eighth in the fake catalog"
    assert t.grounded is True
    assert t.next_action == "show_products"
    assert t.sources

    # 5 — and the recovered state holds for a second, narrower ask.
    t = c.say("nice. and is there a cart under 25")
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["price_max"] == 25.0
    assert t.pick_names == ["Avitas GSC 0.5g Cart"]
    for pick in t.picks:
        assert pick["price_otd"] > 0
        assert "cost" not in pick and "margin" not in pick

    assert len(c.turns) == 5
    assert len(fake_bt.calls["search"]) == 5, "every product turn hit the client, misses included"


@pytest.mark.django_db
def test_sold_out_streak_ends_in_a_human_not_a_guess(convo, fake_bt):
    """The caller gives up on the empty shelf and asks for a person — escalation outranks the miss."""
    c = convo(store="pullman", phone="+15095551234")
    fake_bt.fail_search = True

    t = c.say("do you have any live resin dabs today")
    assert_honest_empty(t, fake_bt)

    t = c.say("nothing at all? what about wax or hash")
    assert t.args("suggest_products")["category"] == "concentrate"
    assert_honest_empty(t, fake_bt)

    # The two misses above never escalated; this one does, on the caller's words alone.
    t = c.say("come on. is there a budtender who can actually go look at the shelf")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True
    assert t.next_action == "escalate"
    assert "suggest_products" not in t.tools, "an escalation must not be answered with products"

    # Stock is back — but this turn is a GAP, asserted exactly as it behaves rather than papered
    # over. The router's category lexicon is singular-only ("concentrate", not "concentrates"), so
    # no category is derived, no inventory search is attempted, and the FAQ answer is served
    # CONFIDENTLY (grounded, with sources) even though it has nothing to do with concentrates.
    fake_bt.fail_search = False
    searches_before = len(fake_bt.calls["search"])
    t = c.say("okay, while I have you — what concentrates are actually on the shelf")
    assert t.intent == "greeting_other"
    assert t.tools == ["faq_lookup"], "the plural killed the product route entirely"
    assert len(fake_bt.calls["search"]) == searches_before, "no product search was even attempted"
    assert t.picks == []
    assert t.grounded is True and t.sources, "answered confidently from the KB"
    assert t.next_action == "answer"
    assert "concentrate" not in t.answer.lower(), "and the confident answer is off-topic"

    # The caller rephrases in the singular and the same shelf answers immediately.
    t = c.say("I mean rosin or wax, whatever concentrate you've got")
    assert t.intent == "product_suggestion"
    assert t.escalated is False
    assert t.next_action == "show_products"
    assert t.pick_names == ["DOH Compliant RSO 1g", "Live Rosin 1g"]
    assert t.grounded is True

    assert len(c.turns) == 5
