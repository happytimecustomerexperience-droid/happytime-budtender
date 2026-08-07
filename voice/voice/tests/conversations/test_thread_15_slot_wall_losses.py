"""Thread 15 — the slot wall: proof the router's derived slots now survive to budtender.

FIXED 2026-08-07: three routing bugs used to lose slots at the tool-arg wall — a rich effect
(sleep/pain relief/anxiety relief/focused) got silently dropped by ``_sanitize_args`` because
``TOOL_SPECS`` only enums {relaxed, uplifted, middle} (fix 3, ``_EFFECT_TO_BUDTENDER`` maps the
richer value down before dispatch); plural category words ("concentrates", "edibles") missed the
singular-only ``_CATEGORY_RE`` and fell through to the FAQ (fix 2, the regex is plural-tolerant
now); and "pre-roll" was a category chat.py derived but the enum didn't allow, so the handler
bailed on its own required-field check (fix 4, the enum plus budtender's CATEGORY_BY_SLOTKEY).
This module is the regression guard: it pins that all three slots now reach budtender instead of
being thrown away.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_sleep_request_survives_to_budtender(convo, fake_bt):
    """"Something for sleep" is the most common ask in the store — the reason now survives."""
    c = convo(store="yakima")
    t = c.say("I need some flower that helps me sleep")

    assert t.intent == "product_suggestion"
    # The router understands the request AND the mapped value reaches the tool: chat.py maps
    # "sleep" -> "relaxed" via _EFFECT_TO_BUDTENDER (fix 3) before the enum wall ever sees it, so
    # nothing is silently dropped.
    assert t.args("suggest_products")["effect_desired"] == "relaxed"
    sent = fake_bt.calls["search"][-1]["slots"]
    assert sent["effect_desired"] == "relaxed", "the mapped effect now reaches budtender's ranker"
    assert t.picks


@pytest.mark.django_db
@pytest.mark.parametrize("effect,phrase,mapped", [
    ("sleep", "flower to help me sleep", "relaxed"),
    ("focused", "flower that keeps me focused", "uplifted"),
    ("pain relief", "flower for my back pain", "relaxed"),
    ("anxiety relief", "flower for anxiety", "relaxed"),
])
def test_every_derived_effect_survives_via_the_budtender_map(convo, fake_bt, effect, phrase, mapped):
    """Every effect chat.py derives now reaches budtender — mapped to budtender's only known
    vocabulary {relaxed, uplifted, middle} by ``_EFFECT_TO_BUDTENDER`` (fix 3), instead of being
    dropped by the enum wall the way four of the five derived effects used to be."""
    c = convo(store="yakima")
    t = c.say(phrase)
    assert t.args("suggest_products")["effect_desired"] == mapped, f"{effect!r} should map to {mapped!r}"
    assert fake_bt.calls["search"][-1]["slots"]["effect_desired"] == mapped


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
def test_plural_category_words_route_to_products(convo, plural, singular):
    """``_CATEGORY_RE`` is plural-tolerant now (fix 2) — every category pattern spells out both
    the singular and the plural, so the way customers actually speak routes to products exactly
    like the singular does, instead of falling through to the FAQ."""
    plural_turn = convo(store="yakima").say(plural)
    assert plural_turn.intent == "product_suggestion"
    assert plural_turn.picks

    singular_turn = convo(store="yakima").say(singular)
    assert singular_turn.intent == "product_suggestion"
    assert singular_turn.picks
    assert plural_turn.pick_names == singular_turn.pick_names, "plural and singular route identically"


@pytest.mark.django_db
def test_pre_roll_request_reaches_the_catalog(convo, fake_bt):
    """"pre-roll" is a category chat.py derives, and it's now in TOOL_SPECS' category enum and
    budtender's CATEGORY_BY_SLOTKEY (fix 4) — so a caller asking for the cheapest joint gets a
    real answer instead of being told nothing is in stock while pre-rolls sit on the shelf."""
    c = convo(store="yakima")
    t = c.say("what's the cheapest pre-roll you have")

    assert t.intent == "product_suggestion"
    assert t.args("suggest_products")["category"] == "pre-roll"  # router got it right
    assert t.result("suggest_products").get("error") is None
    assert t.pick_names == ["Single Pre-roll 1g", "Half Ounce Pre-roll 5pk"]
    assert t.next_action == "show_products"
    assert t.grounded is True
    # budtender is asked, and the fixture catalog's two pre-rolls are exactly what comes back.
    assert fake_bt.calls["search"][-1]["slots"]["category"] == "pre-roll"
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
