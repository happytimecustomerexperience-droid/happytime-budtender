"""Thread 23 — lab data on the pick, and a "stronger / cheaper / different" that keeps the thread.

budtender now returns real lab data (terpenes with %, THC/CBD, screens, COA), allowlisted product
facts and a ``size`` on every pick, and honours a ``sort_by`` search slot. This thread proves the
shared brain (``answer_text_chat`` + the suggest tools the Vapi squad also calls) carries all of it
and never invents a number:

  (a) an eighth under $50 -> the pick speaks its size, THC and top terpenes, every figure a tool value
  (b) "something a bit stronger" keeps category + budget + size + effect, sorts by potency, excludes
      nothing, and says so honestly when nothing has higher THC
  (c) a pick with no lab on file carries and speaks no terpene/lab figure at all
  (d) an edible's allergens ride verbatim and are never inferred from its name
  (e) "which has more myrcene" is answered from the picks' own numbers
  (f) a figure the tools never returned is never spoken

The ``FakeBudtender`` in conftest HONOURS ``sort_by`` (a slot that is not routed leaves the order
alone and fails here) and re-serves the same lab/info/size on a by-sku lookup, like the real view.
"""

from __future__ import annotations

import json
import re

import pytest

_NUM = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text) -> set[float]:
    """Every figure in a line, as a number (so "2 percent" matches the payload's 2.0)."""
    return {float(n) for n in _NUM.findall(str(text or ""))}


def _lab(terps, *, thc=None, cbd=None, total=None, line="", coa=None, screens=None, minors=None):
    return {
        "total_terpenes": total,
        "terpenes": [{"name": n, "pct": p} for n, p in terps],
        "cbd_total": cbd, "thc_total": thc,
        "minor_cannabinoids": minors or [],
        "tested_date": "2026-08-27", "lab_name": None, "coa_url": coa,
        "contaminants": screens or {},
        "profile": {"lean": None, "notes": [], "line": line},
    }


def _flower(sku, name, brand, price, thc, lab, why, **extra):
    row = {"sku": sku, "name": name, "brand": brand, "strain": name.rsplit(" ", 1)[0],
           "category": "flower", "subcategory": "hybrid", "size": "3.5g", "price": price,
           "thc_percent": thc, "dominant_terpene": None, "stock_on_hand": 10, "why_this": why,
           "lab": lab, "info": None}
    row.update(extra)
    return row


def _shelf():
    """Five in-budget eighths with distinct brands/strains (so dedupe keeps them) + two that must
    never show up for 'an eighth under $50' (an ounce, and a $65 reserve)."""
    return [
        _flower("FL-GG4", "Gorilla Glue 3.5g", "Grow Op", 30.0, 24.1,
                _lab([("Beta-Caryophyllene", 1.1), ("Beta-Myrcene", 0.5), ("Limonene", 0.3)],
                     total=2.4, line="Caryophyllene-led (1.1%) — pepper, spice."),
                "Balanced hybrid, easy all-rounder"),
        _flower("FL-BBOG", "Blueberry OG 3.5g", "Phat Panda", 38.0, 27.3,
                _lab([("Beta-Myrcene", 2.0), ("Beta-Caryophyllene", 0.9), ("Limonene", 0.4)],
                     total=3.9, line="Myrcene-led (2%) — earthy, musky; often described as relaxing."),
                "Indica-leaning and mellow"),
        _flower("FL-LEMON", "Lemon Tree 3.5g", "Citrus Co", 42.0, 22.0,
                _lab([("Limonene", 1.2), ("Alpha-Pinene", 0.3)], total=1.9,
                     line="Limonene-led (1.2%) — citrus; often described as uplifting."),
                "Bright daytime flower"),
        _flower("FL-PURPLE", "Purple Punch 3.5g", "Berry Farms", 45.0, 25.5,
                _lab([("Linalool", 0.8), ("Beta-Myrcene", 0.7)], total=2.2,
                     line="Linalool-led (0.8%) — floral, lavender."),
                "Evening favorite"),
        _flower("FL-GAS", "Gas Mask 3.5g", "Cloud 9", 48.0, 31.9,
                _lab([("Limonene", 1.8), ("Beta-Caryophyllene", 0.7)], total=3.1,
                     line="Limonene-led (1.8%) — citrus; often described as uplifting."),
                "Big citrus flavor"),
        _flower("FL-OZ", "Ounce Deal 28g", "Value Farm", 99.0, 22.0, None, "Ounce deal", size="28g"),
        _flower("FL-RES", "Reserve Cut 3.5g", "Top Shelf Co", 65.0, 35.0,
                _lab([("Limonene", 2.2)], total=3.0, line="Limonene-led (2.2%) — citrus."),
                "Top shelf"),
    ]


@pytest.fixture
def shelf(fake_bt):
    fake_bt.catalog = _shelf()
    return fake_bt


def _spoken_numbers_are_the_tools(t, fake_bt) -> None:
    """Numbers-Guard: every figure in the spoken line is one the budtender payload carried."""
    assert _numbers(t.answer) <= _numbers(json.dumps(fake_bt.catalog)), t.answer


# ── (a) an eighth under $50 ──────────────────────────────────────────────────────
@pytest.mark.django_db
def test_an_eighth_under_fifty_speaks_size_thc_and_terpenes_from_the_tool(convo, shelf):
    c = convo(store="yakima")
    t = c.say("something relaxing, an eighth of flower under $50")

    args = t.args("suggest_products")
    assert args["category"] == "flower" and args["size"] == "3.5g" and args["price_max"] == 50.0
    assert t.pick_names == ["Gorilla Glue 3.5g", "Blueberry OG 3.5g", "Lemon Tree 3.5g"]

    top = t.picks[0]
    assert top["size"] == "3.5g"
    assert top["thc_spoken"] == "24.1 percent THC"
    assert top["terpenes_spoken"] == (
        "caryophyllene at 1.1 percent, myrcene at 0.5 percent, limonene at 0.3 percent"
    )
    assert top["profile_line"] == "Caryophyllene-led (1.1%) — pepper, spice."
    assert "lab" not in top and "info" not in top  # raw payloads are never copied through

    # ...and the line the caller hears carries them, with nothing composed.
    assert "24.1 percent THC" in t.answer
    assert "caryophyllene at 1.1 percent" in t.answer
    assert "30 dollars out the door" in t.answer
    _spoken_numbers_are_the_tools(t, shelf)
    assert t.grounded is True


# ── (b) "something a bit stronger" keeps the thread ──────────────────────────────
@pytest.mark.django_db
def test_stronger_keeps_category_budget_size_and_effect_and_sorts_by_potency(convo, shelf):
    c = convo(store="yakima")
    c.say("something relaxing, an eighth of flower under $50")
    t = c.say("something a bit stronger")

    assert t.args("suggest_products") == {
        "category": "flower", "size": "3.5g", "price_max": 50.0, "effect_desired": "relaxed",
        "sort_by": "potency", "store": "yakima",
    }
    sent = shelf.calls["search"][-1]
    assert sent["slots"]["sort_by"] == "potency", "the slot wall dropped sort_by"
    assert sent["slots"]["price_max"] == 50.0 and sent["slots"]["size"] == "3.5g"
    assert not sent["exclude_skus"], "'stronger' must not hide what was already shown"

    # Strongest within budget first; the $65 reserve (35% THC) is over budget and never offered.
    assert t.pick_names == ["Gas Mask 3.5g", "Blueberry OG 3.5g", "Purple Punch 3.5g"]
    assert [p["thc_percent"] for p in t.picks] == [31.9, 27.3, 25.5]
    # The reply acknowledges the thread: it compares against what it said last (24.1), it does not
    # re-introduce itself.
    assert t.answer.startswith("Stepping up from the Gorilla Glue 3.5g at 24.1 percent THC")
    assert "a higher-THC pick that fits is the Cloud 9 Gas Mask 3.5g" in t.answer
    assert "31.9 percent THC" in t.answer and "My top pick is" not in t.answer
    _spoken_numbers_are_the_tools(t, shelf)


@pytest.mark.django_db
def test_stronger_says_so_when_the_top_pick_is_already_the_strongest(convo, shelf):
    c = convo(store="yakima")
    c.say("an eighth of flower under $50")
    c.say("something stronger")
    t = c.say("anything even stronger than that")

    assert t.args("suggest_products")["price_max"] == 50.0  # the budget survived two refinements
    assert t.pick_names[0] == "Gas Mask 3.5g"
    assert t.answer == (
        "I don't have anything with higher THC than the Gas Mask 3.5g at 31.9 percent THC that fits "
        "what you've asked for."
    )  # a comparison of two THC figures — never a "strongest" claim about the product
    assert not shelf.calls["search"][-1]["exclude_skus"]
    assert "Reserve Cut" not in " ".join(t.pick_names)


@pytest.mark.django_db
def test_stronger_with_nothing_to_compare_against_makes_no_comparison_claim(convo, shelf):
    c = convo(store="yakima")
    t = c.say("what's something stronger in flower under $50")  # first message: nothing came before

    assert t.args("suggest_products")["sort_by"] == "potency"
    assert t.pick_names[0] == "Gas Mask 3.5g"
    assert t.answer.startswith("Going by THC, the top pick that fits is the Cloud 9 Gas Mask 3.5g")
    assert "Stepping up" not in t.answer and "higher THC than" not in t.answer


@pytest.mark.django_db
def test_cheaper_keeps_the_budget_and_asks_budtender_to_sort_by_price(convo, shelf):
    c = convo(store="yakima")
    c.say("something relaxing, an eighth of flower under $50")
    t = c.say("something cheaper")

    assert t.args("suggest_products") == {
        "category": "flower", "size": "3.5g", "price_max": 50.0, "effect_desired": "relaxed",
        "sort_by": "price_asc", "store": "yakima",
    }
    assert shelf.calls["search"][-1]["slots"]["sort_by"] == "price_asc"
    assert not shelf.calls["search"][-1]["exclude_skus"]
    assert t.answer.startswith("The lowest price I have that fits is the")


@pytest.mark.django_db
def test_a_budget_from_an_earlier_category_is_not_carried_into_a_new_one(convo, shelf):
    shelf.catalog += [{"sku": "CT-X", "name": "Test Cart 1g", "brand": "Cart Co", "strain": "X",
                       "category": "cartridge", "subcategory": "hybrid", "size": "1g", "price": 25.0,
                       "thc_percent": 80.0, "dominant_terpene": None, "stock_on_hand": 5,
                       "why_this": "Clean cart", "lab": None, "info": None}]
    c = convo(store="yakima")
    c.say("a cart under $30")
    c.say("actually an eighth of flower instead")
    t = c.say("something stronger")

    args = t.args("suggest_products")
    assert args["category"] == "flower" and args["size"] == "3.5g"
    assert "price_max" not in args, "the cart budget must not follow the caller onto flower"


@pytest.mark.django_db
def test_stronger_never_compares_against_a_pick_from_another_shelf(convo, shelf):
    shelf.catalog += [{"sku": "CT-X", "name": "Test Cart 1g", "brand": "Cart Co", "strain": "X",
                       "category": "cartridge", "subcategory": "hybrid", "size": "1g", "price": 25.0,
                       "thc_percent": 80.0, "dominant_terpene": None, "stock_on_hand": 5,
                       "why_this": "Clean cart", "lab": None, "info": None}]
    c = convo(store="yakima")
    c.say("a cart under $30")
    t = c.say("actually, back to flower, anything stronger")

    assert t.args("suggest_products")["category"] == "flower"
    assert "check_inventory" not in t.tools  # nothing to compare: the last pick was a cart
    assert t.answer.startswith("Going by THC, the top pick that fits is the")
    assert "Stepping up" not in t.answer and "80" not in t.answer


@pytest.mark.django_db
def test_a_follow_up_the_carried_slots_cannot_meet_is_relaxed_out_loud_not_dropped_silently(convo, fake_bt):
    fake_bt.catalog = [
        {"sku": "ED-CQ", "name": "Cannaquench Sparkling 5mg", "brand": "Cannaquench", "strain": None,
         "category": "edible", "subcategory": "beverage", "size": "5mg", "price": 8.0,
         "thc_percent": None, "dominant_terpene": None, "stock_on_hand": 30, "why_this": "Microdose drink",
         "lab": None, "info": None},
        {"sku": "ED-WYLD", "name": "Wyld Raspberry Gummies 10mg", "brand": "Wyld", "strain": None,
         "category": "edible", "subcategory": "gummies", "size": "10mg", "price": 15.0,
         "thc_percent": None, "dominant_terpene": None, "stock_on_hand": 40, "why_this": "Low-dose",
         "lab": None, "info": None},
    ]
    c = convo(store="yakima")
    c.say("edibles under $10")
    t = c.say("actually stronger, like 10mg")

    searches = fake_bt.calls["search"]
    assert len(searches) == 3  # turn 1, then this turn: the carried-budget try, then the relaxed try
    assert searches[1]["slots"]["price_max"] == 10.0, "the earlier budget was tried first"
    assert "price_max" not in searches[2]["slots"] and "price_max" not in t.args("suggest_products")
    assert t.pick_names == ["Wyld Raspberry Gummies 10mg"]
    assert t.answer.startswith("I don't have anything that fits everything you've told me so far")
    assert "The closest I have is the Wyld Raspberry Gummies 10mg" in t.answer  # an edible: no THC claim
    assert "higher" not in t.answer


@pytest.mark.django_db
def test_a_correction_takes_back_what_came_before_it(convo, shelf):
    c = convo(store="yakima")
    c.say("indica flower under $50")
    c.say("sorry, I meant the flower options, not that")
    t = c.say("something cheaper")

    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert "subcategory" not in args and "price_max" not in args  # both were said before the correction


@pytest.mark.django_db
def test_different_excludes_what_was_shown_and_stronger_does_not(convo, shelf):
    c = convo(store="yakima")
    first = c.say("an eighth of flower under $50")
    shown = [p["sku"] for p in first.picks]
    assert shown == ["FL-GG4", "FL-BBOG", "FL-LEMON"]

    t = c.say("got something different")
    assert shelf.calls["search"][-1]["exclude_skus"] == shown, "the slot wall dropped exclude_skus"
    assert t.pick_names == ["Purple Punch 3.5g", "Gas Mask 3.5g"]
    assert t.args("suggest_products")["price_max"] == 50.0
    assert t.answer.startswith("Another option is the")


@pytest.mark.django_db
def test_different_with_nothing_left_says_so_instead_of_a_dead_end(convo, shelf):
    shelf.catalog = [r for r in shelf.catalog if r["sku"] in {"FL-GG4", "FL-BBOG", "FL-LEMON"}]
    c = convo(store="yakima")
    c.say("an eighth of flower under $50")
    t = c.say("any other options")

    assert t.picks == []
    assert "don't have anything else that fits" in t.answer
    assert "can't find any matching items" not in t.answer
    assert not _numbers(t.answer), t.answer  # an ungrounded line invents no figure


# ── (c) no lab on file -> no lab figure anywhere ─────────────────────────────────
@pytest.mark.django_db
def test_a_pick_with_no_lab_speaks_no_terpene_or_lab_figure(convo, shelf):
    shelf.catalog = [
        _flower("FL-A", "House Shake 3.5g", "Value Farm", 20.0, 19.0, None, "Everyday value",
                dominant_terpene="Pinene"),
        _flower("FL-B", "Corner Store Haze 3.5g", "Lowland", 26.0, 21.0, None, "Easy sativa"),
    ]
    c = convo(store="yakima")
    t = c.say("an eighth of flower")

    top = t.picks[0]
    for key in ("terpenes", "terpenes_spoken", "total_terpenes_spoken", "minor_cannabinoids_spoken",
                "profile_line", "lab_screens_passed", "tested_date", "cbd_spoken", "coa_url"):
        assert key not in top, key
    assert "terpene" not in t.answer.lower() and "pinene" not in t.answer.lower()
    assert "19 percent THC" in t.answer  # the inventory potency is still real and still spoken
    assert _numbers(t.answer) <= {3.5, 19.0, 20.0}, t.answer

    # Asked to compare terpenes with nothing on file, it says so — no figure but the sizes in the names.
    t = c.say("which one has more myrcene")
    assert _numbers(t.answer) <= {3.5}, t.answer
    assert "don't have lab results on file" in t.answer
    assert t.grounded is False


# ── (d) allergens: verbatim when on file, never inferred ─────────────────────────
@pytest.mark.django_db
def test_allergens_ride_verbatim_and_a_name_never_implies_one(convo, fake_bt):
    allergens = "Contains: Milk, Soy Lecithin. May contain TREE NUTS (almond)."
    fake_bt.catalog = [
        {"sku": "ED-WYLD", "name": "Wyld Raspberry Gummies 10mg", "brand": "Wyld", "strain": None,
         "category": "edible", "subcategory": "gummies", "size": "10mg", "price": 15.0,
         "thc_percent": None, "dominant_terpene": None, "stock_on_hand": 40,
         "why_this": "Low-dose, predictable", "lab": None, "info": {"allergens": allergens}},
        {"sku": "ED-PB", "name": "Peanut Butter Crunch Chews 5mg", "brand": "Crunch Co", "strain": None,
         "category": "edible", "subcategory": "chews", "size": "5mg", "price": 18.0,
         "thc_percent": None, "dominant_terpene": None, "stock_on_hand": 12,
         "why_this": "Chewy and mild", "lab": None, "info": {"strain_type": "Hybrid"}},
    ]
    c = convo(store="yakima")
    t = c.say("got any gummies or chews")

    by_sku = {p["sku"]: p for p in t.picks}
    assert by_sku["ED-WYLD"]["allergens"] == allergens  # byte-for-byte, casing and all
    assert "allergens" not in by_sku["ED-PB"]  # a peanut in the NAME is not an allergen on file
    assert "nut" not in t.answer.lower()
    assert "thc_spoken" not in by_sku["ED-WYLD"]  # an edible with no potency number speaks none
    assert not (_numbers(t.answer) - {10.0, 5.0, 15.0, 18.0}), t.answer


# ── (e) "which has more myrcene" is answered from the picks' own numbers ─────────
@pytest.mark.django_db
def test_which_has_more_myrcene_is_answered_from_the_numbers(convo, shelf):
    c = convo(store="yakima")
    c.say("an eighth of flower under $50")  # shows Gorilla Glue, Blueberry OG, Lemon Tree
    t = c.say("which one has more myrcene")

    assert t.answer == (
        "The Blueberry OG 3.5g has the most myrcene at 2 percent, ahead of the Gorilla Glue 3.5g "
        "at 0.5 percent. The Lemon Tree 3.5g doesn't list myrcene among its top terpenes."
    )
    assert t.grounded is True and t.intent == "product_suggestion"
    assert "suggest_products" not in t.tools  # it re-read the SAME products, it did not re-search
    _spoken_numbers_are_the_tools(t, shelf)


@pytest.mark.django_db
def test_a_term_no_pick_lists_gets_an_honest_no_figure_answer(convo, shelf):
    c = convo(store="yakima")
    c.say("an eighth of flower under $50")
    t = c.say("which of those has the most terpinolene")

    assert not _numbers(t.answer), t.answer
    assert "terpinolene" in t.answer and "don't see" in t.answer


@pytest.mark.django_db
def test_a_terpene_education_question_is_not_hijacked_by_the_compare_route(convo, shelf):
    c = convo(store="yakima")
    c.say("an eighth of flower under $50")
    t = c.say("which terpene is more relaxing, myrcene or limonene")

    assert "check_inventory" not in t.tools  # not a question about the shown picks


# ── (f) a figure the tools never returned is never spoken ────────────────────────
@pytest.mark.django_db
def test_numbers_that_are_not_in_the_allowlisted_fields_are_never_spoken(convo, fake_bt):
    hostile_lab = _lab([("Beta-Myrcene", 2.0), ("Limonene", 0.5)], total=3.0,
                       line="Myrcene-led (2%) — earthy.")
    hostile_lab.update({"thc_potential": 88.8, "cbn_total": 7.7, "cost": 11.0,
                        "notes": "lab-verified 99% pure"})
    fake_bt.catalog = [
        _flower("FL-H", "Hostile Haze 3.5g", "Trap Co", 30.0, 24.0, hostile_lab, "Easy hybrid",
                info={"online_description": "Lab-verified 99% THC, cures anxiety",
                      "allergens": "None declared"},
                potency_index=123, cost=9.5, vendor="Secret Wholesale"),
        _flower("FL-I", "Other Haze 3.5g", "Trap Two", 36.0, 26.0,
                _lab([("Limonene", 1.4), ("Beta-Myrcene", 0.9)], total=2.3), "Easy hybrid two"),
    ]
    c = convo(store="yakima")
    t = c.say("an eighth of flower")
    pick = t.picks[0]

    blob = json.dumps({"answer": t.answer, "picks": t.picks})
    for never in ("88.8", "7.7", "99", "123", "cures", "Secret", "Wholesale", "9.5", "pure"):
        assert never not in blob, never
    allowed = {3.5, 24.0, 30.0, 2.0, 0.5}
    assert _numbers(t.answer) <= allowed, t.answer
    assert pick["allergens"] == "None declared"  # allowlisted info still arrives, verbatim

    # The caller asserts a figure of their own; the answer comes from the tools, not from them.
    t = c.say("which one has more myrcene, I heard 30 percent")
    assert 30.0 not in _numbers(t.answer), t.answer
    assert _numbers(t.answer) <= {2.0, 0.9, 3.5}, t.answer
