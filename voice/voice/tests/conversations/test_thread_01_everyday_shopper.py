"""Thread 01 — the everyday shopper: hours, specials, a product ask, then a budget.

The bread-and-butter call. Proves the harness itself and the two most common routes
(grounded FAQ, product suggestion) hand off to each other inside one conversation.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_everyday_shopper(convo):
    c = convo(store="yakima")

    t = c.say("hi what are your hours today")
    assert t.intent == "hours_location"
    assert t.grounded, "hours must come from the KB, never invented"
    assert t.sources, "a grounded answer must cite the row it came from"
    assert t.tools == ["faq_lookup"]

    t = c.say("nice, any specials running")
    assert t.intent == "specials"
    assert t.tools == ["faq_lookup"]

    t = c.say("cool. I want some relaxing indica flower")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert args["subcategory"] == "indica"
    assert args["effect_desired"] == "relaxed"
    assert t.picks, "the fake catalog has indica flower — a miss means slots never reached search"
    assert t.next_action == "show_products"

    t = c.say("do you have a cartridge under $40")
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["price_max"] == 40.0
    assert t.picks
    for pick in t.picks:
        assert pick["price_otd"] > 0
        assert "cost" not in pick and "margin" not in pick

    assert len(c.turns) == 4
    assert c.transcript.count("user:") == 4
