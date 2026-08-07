"""Thread 15 — the slot wall: what the router derives but the schema silently throws away.

``voice/chat.py`` derives rich slots from free speech, then ``voice/tools/__init__.py::_sanitize_args``
drops any value that fails ``TOOL_SPECS``' enum. Nothing logs the drop, so the caller's actual
request quietly stops reaching budtender's ranker. These tests pin the CURRENT behaviour so the
loss is visible and a fix has a failing-then-passing test to move.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_sleep_request_loses_its_effect_before_reaching_budtender(convo, fake_bt):
    """"Something for sleep" is the most common ask in the store — and the reason is dropped."""
    c = convo(store="yakima")
    t = c.say("I need some flower that helps me sleep")

    assert t.intent == "product_suggestion"
    # The router understood the request correctly...
    assert t.args("suggest_products")["effect_desired"] == "sleep"
    # ...and the schema wall threw it away, because TOOL_SPECS enums effect_desired to
    # {relaxed, uplifted, middle} while chat.py derives {sleep, relaxed, focused,
    # pain relief, anxiety relief}. Four of the five derived effects cannot survive.
    sent = fake_bt.calls["search"][-1]["slots"]
    assert "effect_desired" not in sent, "FIXED? then tighten this test"
    assert t.picks, "picks still come back — just ranked without knowing why they were asked for"


@pytest.mark.django_db
@pytest.mark.parametrize("effect,phrase", [
    ("sleep", "flower to help me sleep"),
    ("focused", "flower that keeps me focused"),
    ("pain relief", "flower for my back pain"),
    ("anxiety relief", "flower for anxiety"),
])
def test_only_relaxed_survives_the_effect_enum(convo, fake_bt, effect, phrase):
    c = convo(store="yakima")
    t = c.say(phrase)
    assert t.args("suggest_products")["effect_desired"] == effect
    assert "effect_desired" not in fake_bt.calls["search"][-1]["slots"]


@pytest.mark.django_db
def test_relaxed_is_the_one_effect_that_gets_through(convo, fake_bt):
    c = convo(store="yakima")
    t = c.say("some relaxing flower for tonight")
    assert t.args("suggest_products")["effect_desired"] == "relaxed"
    assert fake_bt.calls["search"][-1]["slots"]["effect_desired"] == "relaxed"


@pytest.mark.django_db
def test_an_effect_alone_never_reaches_the_shelf(convo):
    """No category word means no product route at all — "something relaxing for tonight" is
    answered from the FAQ instead of the catalog, because chat.py derives category from the
    CURRENT message only and there is no conversational memory to borrow it from."""
    c = convo(store="yakima")
    t = c.say("something relaxing for tonight")
    assert t.tools == ["faq_lookup"]
    assert t.intent != "product_suggestion"


@pytest.mark.django_db
@pytest.mark.parametrize("plural,singular", [
    ("what concentrates do you have", "what concentrate do you have"),
    ("do you have any edibles", "do you have any edible"),
])
def test_plural_category_words_miss_the_regex(convo, plural, singular):
    """``_CATEGORY_RE`` anchors on ``\\bconcentrate\\b`` / ``\\bedible\\b``, so the way customers
    actually speak — plural — never routes to products. ``cart|carts`` is the only pair that
    spelled both out. The plural ask falls through to the FAQ and answers something unrelated."""
    plural_turn = convo(store="yakima").say(plural)
    assert plural_turn.tools == ["faq_lookup"], "FIXED? then invert this test"
    assert plural_turn.intent != "product_suggestion"

    singular_turn = convo(store="yakima").say(singular)
    assert singular_turn.intent == "product_suggestion"
    assert singular_turn.picks


@pytest.mark.django_db
def test_pre_roll_request_cannot_reach_the_catalog_at_all(convo, fake_bt):
    """``pre-roll`` is a category chat.py derives but TOOL_SPECS does not allow — so the
    category is dropped, the handler then fails its own required-field check, and a caller
    asking for the cheapest joint is told nothing is in stock while pre-rolls sit on the shelf."""
    c = convo(store="yakima")
    t = c.say("what's the cheapest pre-roll you have")

    assert t.intent == "product_suggestion"
    assert t.args("suggest_products")["category"] == "pre-roll"  # router got it right
    assert t.result("suggest_products").get("error") == "missing_category"
    assert t.picks == []
    assert t.next_action == "ask_staff"
    # budtender is never even asked, though the fixture catalog holds two pre-rolls.
    assert not fake_bt.calls.get("search"), "search was called after all — re-check the wall"
    assert any(row["category"] == "pre-roll" for row in fake_bt.catalog)


@pytest.mark.django_db
def test_the_four_enum_categories_do_reach_the_catalog(convo, fake_bt):
    """Control: the categories TOOL_SPECS allows behave, so the pre-roll failure is the enum."""
    for phrase, category in (
        ("show me some flower", "flower"),
        ("got any gummies", "edible"),
        ("looking for a cartridge", "cartridge"),
        ("what concentrate do you have", "concentrate"),
    ):
        c = convo(store="yakima")
        t = c.say(phrase)
        diag = f"{phrase!r} -> intent={t.intent} tools={t.tools} answer={t.answer[:70]!r}"
        assert t.args("suggest_products").get("category") == category, diag
        assert fake_bt.calls["search"][-1]["slots"]["category"] == category, diag
        assert t.picks, f"{category} should return picks from the fixture catalog; {diag}"
