"""Thread 24 — a price is per SIZE, so a price ask runs through the questions (2026-10-06).

The owner's rule: "any price must still ask for the size and the other questions we ask during the
exploratory". The guarantee is CODE, in ``voice/tools/suggest.py`` (``needs_size``): a search for a
size-required category with no ``size`` slot returns picks with NO price, ``needs_size`` and the sizes the
shelf really has, and a ``spoken_summary`` that asks. The text brain (``answer_text_chat``) has no price
rule of its own: it speaks that question, reads the caller's answer ("an eighth") as the size of the SAME
search, and only then speaks the price — from the tool — followed by ONE light next question (the scent).

  (a) "how much is flower" -> the size question and no price; "an eighth" -> the tool's price + the scent
      question (one question, never a wall); the scent answer re-ranks with the aroma slot
  (b) a named product's price ask -> size first; the size answer completes THAT product's search
  (c) "a cartridge under forty bucks" -> size first (a budget ceiling is not a size)
  (d) "just the price", twice -> the size question reworded, then a team member — never a number
  (e) a stock-only question is answered without a price; a category with no size concept is unchanged
  (f) "how much was it" re-searches with the remembered slots, so it obeys the gate for free
  (g) the aroma slot reaches the budtender search body from the caller's words and from the website's slots

This thread REPLACES the old "quote straight from suggest_products before the questionnaire" shortcut
that thread 10 (``test_price_tracks_the_tool...``) used to pin.
"""

from __future__ import annotations

import re

import pytest

from voice import constants as C
from voice.tests.conversations.conftest import aroma_lab

_DIGIT = re.compile(r"\d")


def _no_price(t) -> None:
    """Nothing priced anywhere in the turn: not on a pick, not in the reply."""
    assert all("price_otd" not in p and "price_spoken" not in p for p in t.picks), t.picks
    assert "dollar" not in t.answer.lower() and "$" not in t.answer and "out the door" not in t.answer


def _priced_from_the_tool(t) -> None:
    """The price in the reply is the tool's own wording for the lead pick."""
    top = t.picks[0]
    assert top["price_otd"] > 0 and top["price_spoken"] in t.answer, (t.answer, top)


# ── (a) "how much is flower" ─────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_bare_price_ask_is_answered_with_the_size_question_not_a_number(convo, fake_bt):
    c = convo(store="yakima")
    t = c.say("how much is flower")

    assert t.intent == "product_suggestion"
    assert t.args("suggest_products")["category"] == "flower" and "size" not in t.args("suggest_products")
    assert t.result("suggest_products")["needs_size"] is True
    assert t.result("suggest_products")["size_options"] == ["3.5g", "28g"]  # the sizes the shelf has
    assert t.answer == "Prices depend on the size — are you thinking an eighth or an ounce?"
    assert not _DIGIT.search(t.answer)
    _no_price(t)
    assert t.picks, "the products are still on the result — only the price is withheld"


@pytest.mark.django_db
def test_the_size_answer_completes_the_same_search_then_one_scent_question(convo, fake_bt):
    c = convo(store="yakima")
    c.say("how much is flower")
    t = c.say("an eighth")

    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "flower" and args["size"] == "3.5g", "the answer was not carried onto the shelf"
    assert fake_bt.calls["search"][-1]["slots"]["size"] == "3.5g"
    _priced_from_the_tool(t)
    assert t.picks[0]["price_otd"] == 30.0 and "30 dollars out the door" in t.answer
    # ONE light next question from the flow — the scent — and nothing else (never a wall of questions).
    assert t.answer.endswith(C.AROMA_QUESTION)
    assert t.answer.count("?") == 1


@pytest.mark.django_db
def test_the_scent_answer_reaches_the_search_and_is_not_asked_again(convo, fake_bt):
    fake_bt.catalog = [dict(r) for r in fake_bt.catalog]
    for row in fake_bt.catalog:
        if row["sku"] == "FL-BBOG-35":
            row["lab"] = aroma_lab(
                [("Limonene", 1.2)],
                "Smells citrusy (limonene). Customers often describe profiles like this as uplifting — "
                "everyone is different.",
                ["citrus"],
            )
    c = convo(store="yakima")
    c.say("how much is flower")
    first = c.say("an eighth")
    assert first.picks[0]["name"] == "Gorilla Glue #4 3.5g"  # cheapest eighth leads without a scent

    t = c.say("citrusy")
    args = t.args("suggest_products")
    assert args["aroma"] == "citrus" and args["size"] == "3.5g" and args["category"] == "flower"
    sent = fake_bt.calls["search"][-1]["slots"]
    assert sent["aroma"] == "citrus" and sent["size"] == "3.5g", "the aroma slot never reached budtender"
    assert t.picks[0]["name"] == "Blueberry OG 3.5g", "budtender's aroma nudge puts the citrus pick first"
    assert t.picks[0]["profile_explain"].startswith("Smells citrusy (limonene)")
    _priced_from_the_tool(t)
    assert C.AROMA_QUESTION not in t.answer, "the scent question is asked once per session"


@pytest.mark.django_db
def test_a_brush_off_to_the_scent_question_keeps_the_pick_and_asks_nothing_more(convo, fake_bt):
    c = convo(store="yakima")
    c.say("how much is flower")
    c.say("an eighth")
    t = c.say("no preference")

    assert t.intent == "product_suggestion"
    assert "aroma" not in t.args("suggest_products"), "'no preference' leaves the slot out"
    assert t.args("suggest_products")["size"] == "3.5g"
    _priced_from_the_tool(t)
    assert "?" not in t.answer


@pytest.mark.django_db
@pytest.mark.parametrize("answer,size", [("a gram", "1g"), ("a half gram", "0.5g"), ("half gram", "0.5g")])
def test_the_words_of_the_question_are_understood_as_the_answer(convo, answer, size):
    c = convo(store="yakima")
    q = c.say("how much is a cartridge")
    assert q.answer == "Prices depend on the size — are you thinking a half gram or a gram?"
    t = c.say(answer)
    assert t.args("suggest_products")["size"] == size
    _priced_from_the_tool(t)


# ── (b) a named product ──────────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_named_products_price_ask_asks_the_size_and_the_answer_completes_that_product(convo, fake_bt):
    c = convo(store="yakima")
    q = c.say("how much is the Blueberry OG")

    assert q.intent == "product_suggestion"
    assert q.args("suggest_products")["brand"] == "Blueberry OG" and "size" not in q.args("suggest_products")
    assert q.result("suggest_products")["needs_size"] is True
    assert q.answer == "Prices depend on the size — are you thinking an eighth?"
    _no_price(q)

    t = c.say("an eighth")
    sent = fake_bt.calls["search"][-1]["slots"]
    assert sent["brand"] == "Blueberry OG" and sent["size"] == "3.5g", "the named product was lost"
    assert t.pick_names == ["Blueberry OG 3.5g"]
    assert t.picks[0]["price_otd"] == 38.0 and "38 dollars out the door" in t.answer


# ── (c) a budget is not a size ───────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_a_cartridge_under_forty_asks_the_size_first(convo, fake_bt):
    c = convo(store="yakima")
    q = c.say("how much is a cartridge under forty bucks")

    assert q.args("suggest_products")["price_max"] == 40.0 and "size" not in q.args("suggest_products")
    assert q.answer == "Prices depend on the size — are you thinking a half gram or a gram?"
    _no_price(q)

    t = c.say("half gram")
    sent = fake_bt.calls["search"][-1]["slots"]
    assert sent["price_max"] == 40.0 and sent["size"] == "0.5g", "the ceiling was dropped on the answer"
    assert t.pick_names == ["Avitas GSC 0.5g Cart"]
    assert t.picks[0]["price_otd"] == 22.0 and "22 dollars out the door" in t.answer


# ── (d) pushing for "just the price" ─────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_just_the_price_twice_offers_a_team_member_and_never_a_number(convo, fake_bt):
    c = convo(store="yakima")
    first = c.say("how much is flower")
    second = c.say("just the price")
    third = c.say("I said just give me the price")

    assert first.answer.startswith("Prices depend on the size")
    # Once more, in different words — still the question, still no price.
    assert second.answer.startswith("I want to give you the right price")
    assert "Prices depend on the size" not in second.answer and second.answer.endswith("an eighth or an ounce?")
    assert second.next_action == "show_products"
    # Then a person.
    assert "team member" in third.answer and not _DIGIT.search(third.answer)
    assert third.next_action == "ask_staff" and third.grounded is False
    for t in (first, second, third):
        _no_price(t)

    # The conversation recovers: the size is all it needed.
    t = c.say("ok, an eighth")
    assert t.args("suggest_products")["size"] == "3.5g"
    _priced_from_the_tool(t)


# ── (e) stock-only, and a category with no size concept ──────────────────────────────────────
@pytest.mark.django_db
def test_a_stock_only_question_is_answered_without_a_price(convo, fake_bt):
    c = convo(store="yakima")

    t = c.say("do you have any gummies")
    assert t.intent == "product_suggestion" and t.grounded is True
    assert t.answer.startswith("My top pick is the")
    _no_price(t)
    assert "Prices depend on the size" not in t.answer, "they asked what is on the shelf, not the price"

    t = c.say("is the Wyld Raspberry Gummies 10mg in stock")
    assert t.grounded is True and t.answer.startswith("Yes — the Wyld Raspberry Gummies 10mg is")
    assert "check_inventory" in t.tools
    _no_price(t)
    assert "price_otd" not in t.result("check_inventory") and t.result("check_inventory")["needs_size"] is True


@pytest.mark.django_db
def test_a_category_with_no_size_concept_is_priced_as_before(convo, fake_bt):
    fake_bt.catalog = [*fake_bt.catalog, {
        "sku": "TP-BALM-1", "name": "Calm Balm", "brand": "Balm Co", "strain": None, "category": "topical",
        "subcategory": "balm", "size": None, "price": 18.0, "thc_percent": None, "dominant_terpene": None,
        "stock_on_hand": 9, "why_this": "Plain and simple", "lab": None, "info": None,
    }]
    c = convo(store="yakima")
    t = c.say("how much is a topical")

    assert t.args("suggest_products")["category"] == "topical"
    assert "needs_size" not in t.result("suggest_products")
    assert t.picks[0]["price_otd"] == 18.0 and "18 dollars out the door" in t.answer
    assert "Prices depend on the size" not in t.answer


# ── (f) "how much was it" obeys the gate for free ────────────────────────────────────────────
@pytest.mark.django_db
def test_how_much_was_it_remembers_the_size_and_prices_from_the_tool(convo, fake_bt):
    c = convo(store="yakima")
    said = c.say("an eighth of flower")
    t = c.say("how much was it")

    assert t.args("suggest_products")["size"] == "3.5g", "the remembered slots were not re-used"
    assert t.picks[0]["price_otd"] == said.picks[0]["price_otd"] == 30.0
    _priced_from_the_tool(t)
    assert "Prices depend on the size" not in t.answer


@pytest.mark.django_db
def test_how_much_was_it_with_no_size_ever_given_asks_it_and_quotes_nothing(convo, fake_bt):
    c = convo(store="yakima")
    c.say("show me some flower")
    t = c.say("wait, how much was it")

    assert t.result("suggest_products")["needs_size"] is True
    assert t.answer.startswith("Prices depend on the size")
    _no_price(t)


# ── (g) the aroma slot reaches the budtender search body ─────────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize("word,aroma", [
    ("citrusy", "citrus"), ("earthy", "earthy"), ("piney", "pine"), ("floral", "floral"), ("spicy", "spicy"),
])
def test_a_scent_the_caller_names_becomes_the_aroma_slot(convo, fake_bt, word, aroma):
    c = convo(store="yakima")
    t = c.say(f"something {word}, an eighth of flower")

    assert t.args("suggest_products")["aroma"] == aroma
    assert fake_bt.calls["search"][-1]["slots"]["aroma"] == aroma, "the slot wall dropped aroma"


@pytest.mark.django_db
def test_a_product_name_that_reads_like_a_scent_is_not_an_aroma(convo, fake_bt):
    c = convo(store="yakima")
    c.say("an eighth of pineapple express flower")
    assert "aroma" not in fake_bt.calls["search"][-1]["slots"]


@pytest.mark.django_db
def test_the_website_slots_aroma_reaches_the_search_body(convo, fake_bt):
    c = convo(store="yakima", slots={"aroma": "pine"})
    t = c.say("an eighth of flower")

    assert t.args("suggest_products")["aroma"] == "pine"
    assert fake_bt.calls["search"][-1]["slots"]["aroma"] == "pine"
    c = convo(store="yakima", slots={"aroma": "minty"})  # not one of the five: the slot wall drops it
    c.say("an eighth of flower")
    assert "aroma" not in fake_bt.calls["search"][-1]["slots"]
