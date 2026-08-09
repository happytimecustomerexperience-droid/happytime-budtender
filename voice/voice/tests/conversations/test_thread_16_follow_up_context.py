"""Thread 16 — a follow-up is part of the same conversation, not a brand-new question.

Real transcripts showed every follow-up being treated as a fresh query, so the caller who had
just been shown flower and said "keep it under 40 though" was read the state health warning, and
"anything cheaper than that" got the entire return policy. The router derived category from the
CURRENT message only, so a refinement that names no category fell out of the product path
entirely and retrieval answered with whatever row ranked first.

Two fixes are pinned here:
  * a refinement ("under $40", "cheaper", "something smaller") CARRIES the category forward from
    the caller's own recent turns, so it re-runs the product search instead of falling to the FAQ;
  * the topic the router already derives is PASSED to faq_lookup, so retrieval can be constrained
    to the subject the caller actually asked about instead of returning its global best row.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_price_refinement_carries_the_category_forward(convo):
    c = convo(store="yakima")
    first = c.say("I'm looking for some flower that helps me sleep")
    assert first.intent == "product_suggestion"

    second = c.say("keep it under 40 though")
    assert second.intent == "product_suggestion", "the refinement fell out of the product path"
    args = second.args("suggest_products")
    assert args["category"] == "flower", "category was not carried from the previous turn"
    assert args["price_max"] == 40.0
    assert all(p["price_otd"] <= 40.0 for p in second.picks), second.pick_names


@pytest.mark.django_db
@pytest.mark.parametrize("refinement", ["anything cheaper", "something cheaper than that", "got anything smaller"])
def test_bare_refinements_stay_on_the_product_path(convo, refinement):
    c = convo(store="yakima")
    c.say("do you have any carts for daytime")
    turn = c.say(refinement)
    assert turn.intent == "product_suggestion", f"{refinement!r} left the product path"
    assert turn.args("suggest_products")["category"] == "cartridge"


@pytest.mark.django_db
def test_a_new_category_overrides_the_carried_one(convo):
    """Carrying context must never override what the caller just said."""
    c = convo(store="yakima")
    c.say("show me some flower")
    turn = c.say("actually do you have gummies")
    assert turn.args("suggest_products")["category"] == "edible"


@pytest.mark.django_db
def test_a_fresh_question_does_not_inherit_a_stale_category(convo):
    """Only a refinement carries. An unrelated question must not be dragged onto the shelf."""
    c = convo(store="yakima")
    c.say("show me some flower")
    turn = c.say("what are your hours")
    assert turn.intent == "hours_location"
    assert "suggest_products" not in turn.tools


@pytest.mark.django_db
@pytest.mark.parametrize("message,topic", [
    ("what time do you close today", "hours_location"),
    ("where exactly are you located", "hours_location"),
    ("what specials do you have going on", "specials"),
    ("what's your return policy on cannabis products", "return_policy"),
])
def test_the_derived_topic_is_passed_to_retrieval(convo, message, topic):
    """chat.py already classifies the topic; retrieval was never told. Without it, "what time do
    you close today" was answered with the July specials row."""
    turn = convo(store="yakima").say(message)
    assert turn.args("faq_lookup").get("topic") == topic


@pytest.mark.django_db
def test_an_unclassifiable_question_passes_no_topic(convo):
    """An empty topic must mean unconstrained retrieval, not a filter that matches nothing."""
    turn = convo(store="yakima").say("do you sell rolling papers")
    assert turn.args("faq_lookup").get("topic", "") == ""
    assert turn.answer.strip()
