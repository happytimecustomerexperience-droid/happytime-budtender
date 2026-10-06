"""voice/tools/suggest.py — the price gate (2026-10-06): a price is per SIZE.

A ``suggest_products`` / ``check_inventory`` call for a size-required category with no ``size`` slot
must return NO price of any kind (not ``price_otd``, not ``price_spoken``, not a dollar figure riding in
``why_this`` or the summary) and ask the size instead; with a size nothing changes; a category with no
size concept is untouched. The guarantee is code: these tests drive the handlers (and ``dispatch``, the
path the phone agent and the text brain both take) and walk the WHOLE result for a price key or figure.
Offline, budtender stubbed.
"""

from __future__ import annotations

import json

import pytest

from voice import budtender_client
from voice import constants as C
from voice.tools import _sanitize_args, dispatch, suggest

PRICES = {"a": 61.37, "b": 47.29, "c": 93.11}  # distinctive: no THC / size in these rows shares a digit run


def _row(sku, price, size, why="Easy all-rounder", **over):
    row = {
        "rank": 1, "sku": sku, "name": f"Product {sku} {size or ''}".strip(), "brand": f"Brand {sku}",
        "strain": f"Strain {sku}", "price": price, "price_was": None, "thc_percent": 24.1,
        "dominant_terpene": None, "stock_on_hand": 12, "dutchie_link": "/x", "image_url": None,
        "why_this": why, "size": size, "lab": None, "info": None,
    }
    row.update(over)
    return row


def _shelf():
    return [
        _row("a", PRICES["a"], "3.5g", why="On sale — save $9 · Dialed in for relaxed"),
        _row("b", PRICES["b"], "1g"),
        _row("c", PRICES["c"], "28g"),
    ]


class FakeBudtender:
    def __init__(self):
        self.results = _shelf()
        self.check = None
        self.search_calls: list[dict] = []

    def search(self, slots, *, limit=3, phone=None, session_token=None, exclude_skus=None, location=None):
        self.search_calls.append({"slots": dict(slots)})
        return {"results": self.results[:limit]}

    def check_sku(self, store, sku, *, category=None):
        return self.check or {"in_stock": False}

    def pair_for_sku(self, store, anchor_sku, **kw):
        return {"pairing": None, "strength": 0.0}


@pytest.fixture
def fake_bt(monkeypatch):
    fb = FakeBudtender()
    monkeypatch.setattr(budtender_client, "budtender", lambda: fb)
    monkeypatch.setattr(suggest, "budtender", lambda: fb)
    return fb


def _keys(node):
    """Every dict key anywhere in a result."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _keys(value)


def _assert_no_price(out: dict) -> None:
    """No price key at any depth, and no price figure or dollar sign anywhere in the serialized result."""
    assert not [k for k in _keys(out) if "price" in str(k).lower() and k != "size_options"], list(_keys(out))
    blob = json.dumps(out)
    for figure in ("61.37", "47.29", "93.11", "61", "47", "93"):
        assert figure not in blob, (figure, blob)
    assert "$" not in blob and "dollar" not in blob.lower() and "out the door" not in blob.lower(), blob


CTX = {"call_id": "", "store": "yakima"}


# ── the gate: every size-required category, no size -> no price anywhere ─────────────────────
@pytest.mark.parametrize("category", sorted(C.SIZE_REQUIRED_CATEGORIES))
def test_no_price_key_survives_without_a_size_for_every_size_required_category(fake_bt, category):
    out = suggest.handle_suggest_products({"store": "yakima", "category": category}, dict(CTX))

    assert out["needs_size"] is True
    assert [p["sku"] for p in out["picks"]] == ["a", "b", "c"], "the products themselves are still shown"
    assert out["size_options"] == ["1g", "3.5g", "28g"]  # only sizes the shelf has, smallest first
    assert out["spoken_summary"] == (
        "Prices depend on the size — are you thinking a gram, an eighth, or an ounce?"
    )
    _assert_no_price(out)
    # ...and what makes a pick worth hearing about is all still there.
    assert out["picks"][0]["thc_spoken"] == "24.1 percent THC"
    assert out["picks"][0]["why_this"] == "Dialed in for relaxed", "the '$9' sale bit is price-derived"


@pytest.mark.parametrize("category", sorted(C.SIZE_REQUIRED_CATEGORIES))
def test_with_a_size_nothing_changes(fake_bt, category):
    out = suggest.handle_suggest_products({"store": "yakima", "category": category, "size": "3.5g"}, dict(CTX))

    assert "needs_size" not in out and "size_options" not in out
    assert out["picks"][0]["price_otd"] == PRICES["a"]
    assert out["picks"][0]["price_spoken"] == "61 dollars and 37 cents"
    assert out["picks"][0]["why_this"] == "On sale — save $9 · Dialed in for relaxed"
    assert "61 dollars and 37 cents out the door" in out["spoken_summary"]
    assert fake_bt.search_calls[-1]["slots"]["size"] == "3.5g"


@pytest.mark.parametrize("category", sorted(C.SIZE_EXEMPT_CATEGORIES))
def test_a_category_with_no_size_concept_is_untouched(fake_bt, category):
    out = suggest.handle_suggest_products({"store": "yakima", "category": category}, dict(CTX))

    assert "needs_size" not in out and "size_options" not in out
    assert out["picks"][0]["price_otd"] == PRICES["a"]
    assert out["picks"][0]["why_this"] == "On sale — save $9 · Dialed in for relaxed"
    assert "out the door" in out["spoken_summary"]


def test_the_category_sets_are_the_tool_enum_split_in_two():
    """Drift alarm: a category added to the enum must be CLASSIFIED (required or exempt), never silently
    land in neither — the exempt list is spelled out here so adding to the enum fails this test."""
    enum = set(C.TOOL_SPECS["suggest_products"]["parameters"]["properties"]["category"]["enum"])
    assert enum == set(C.PRODUCT_CATEGORIES)
    assert C.SIZE_REQUIRED_CATEGORIES | C.SIZE_EXEMPT_CATEGORIES == enum
    assert not C.SIZE_REQUIRED_CATEGORIES & C.SIZE_EXEMPT_CATEGORIES
    assert C.SIZE_REQUIRED_CATEGORIES == {
        "flower", "concentrate", "cartridge", "edible", "tincture", "pre-roll",
    }
    assert C.SIZE_EXEMPT_CATEGORIES == {"topical", "capsule", "mint", "blunt", "infused-blunt"}
    assert C.TOOL_SPECS["check_inventory"]["parameters"]["properties"]["category"]["enum"] == list(
        C.PRODUCT_CATEGORIES
    )


# ── what does NOT count as a size ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("extra", [
    {"price_max": 40}, {"price_min": 10, "price_max": 40}, {"price_tier": "value"},
    {"size": ""}, {"size": "any"}, {"size": "stock-up"}, {"size": "disposable"}, {"size": " ANY "},
    {"size": None},
])
def test_a_budget_or_a_no_opinion_size_is_not_a_size(fake_bt, extra):
    out = suggest.handle_suggest_products({"store": "yakima", "category": "cartridge", **extra}, dict(CTX))
    assert out["needs_size"] is True
    _assert_no_price(out)


@pytest.mark.parametrize("args", [
    {"brand": "Phat Panda"},  # no category at all (a brand-only search)
    {"effect_desired": "relaxed"},
    {"price_max": 20},
    {"category": "hoverboards", "brand": "x"},  # not in the enum
])
def test_a_blank_or_unknown_category_fails_closed(fake_bt, args):
    out = suggest.handle_suggest_products({"store": "yakima", **args}, dict(CTX))
    assert out["needs_size"] is True
    _assert_no_price(out)


def test_a_cart_alias_is_a_size_required_category(fake_bt):
    out = suggest.handle_suggest_products({"store": "yakima", "category": "vape pen"}, dict(CTX))
    assert out["needs_size"] is True
    _assert_no_price(out)


def test_an_empty_shelf_is_the_honest_miss_not_a_size_question(fake_bt):
    fake_bt.results = []
    out = suggest.handle_suggest_products({"store": "yakima", "category": "flower"}, dict(CTX))
    assert out["picks"] == [] and "needs_size" not in out
    assert "not finding" in out["spoken_summary"].lower()


def test_a_shelf_with_no_stated_sizes_asks_the_open_question(fake_bt):
    """Edibles/tinctures carry no per-unit ``size`` from budtender: nothing is offered that the shelf
    did not state, so the question is open."""
    fake_bt.results = [_row("a", 20.0, None), _row("b", 30.0, None)]
    out = suggest.handle_suggest_products({"store": "yakima", "category": "edible"}, dict(CTX))
    assert out["needs_size"] is True and out["size_options"] == []
    assert out["spoken_summary"] == "Prices depend on the size — what size are you thinking?"


@pytest.mark.parametrize("options,said", [
    (["1g"], "are you thinking a gram?"),
    (["0.5g", "1g"], "are you thinking a half gram or a gram?"),
    (["1g", "3.5g", "7g", "14g", "28g"],
     "are you thinking a gram, an eighth, a quarter, a half ounce, or an ounce?"),
    (["2g", "10mg"], "are you thinking 2 grams or 10 milligrams?"),
    (["5pk"], "are you thinking 5pk?"),
])
def test_the_size_question_is_built_from_the_options_only(options, said):
    assert suggest.size_question(options) == f"Prices depend on the size — {said}"
    assert suggest.is_size_question(suggest.size_question(options))
    assert suggest.is_size_question(suggest.size_question(options, again=True))
    assert suggest.size_question(options, again=True).startswith(suggest.SIZE_REASK)


def test_sizes_are_sorted_by_their_number_not_as_text():
    rows = [{"size": s} for s in ("28g", "3.5g", "1g", "14g", "7g", "0.5g", None, "")]
    assert suggest._size_options(rows) == ["0.5g", "1g", "3.5g", "7g", "14g", "28g"]


# ── check_inventory is no way around the gate ────────────────────────────────────────────────
CHECK = {"in_stock": True, "price_otd": 61.37, "stock_on_hand": 14, "name": "Product a 3.5g",
         "thc_percent": 24.1, "size": "3.5g", "lab": None, "info": None}


@pytest.mark.parametrize("args", [
    {"store": "yakima", "sku": "a"},  # what the text brain's lookups send
    {"store": "yakima", "sku": "a", "category": "flower"},  # a category is not a size
    {"store": "yakima", "sku": "a", "category": "flower", "size": "any"},
    {"store": "yakima", "sku": "a", "size": "stock-up"},  # no category AND no real size: fails closed
])
def test_check_inventory_withholds_the_price_without_a_size(fake_bt, args):
    fake_bt.check = dict(CHECK)
    out = suggest.handle_check_inventory(args, dict(CTX))

    assert out["in_stock"] is True and out["needs_size"] is True
    assert out["name"] == "Product a 3.5g" and out["thc_spoken"] == "24.1 percent THC"
    assert out["qty_band"] == "in stock"
    assert out["size_options"] == ["3.5g"]  # the SKU's own shelf size
    assert out["spoken_summary"] == "Prices depend on the size — are you thinking an eighth?"
    _assert_no_price(out)


@pytest.mark.parametrize("args", [
    {"store": "yakima", "sku": "a", "category": "flower", "size": "3.5g"},
    {"store": "yakima", "sku": "a", "size": "3.5g"},  # the size is what unlocks it; the category only matters without one
])
def test_check_inventory_releases_the_price_with_a_size(fake_bt, args):
    fake_bt.check = dict(CHECK)
    out = suggest.handle_check_inventory(args, dict(CTX))
    assert "needs_size" not in out
    assert out["price_otd"] == 61.37 and out["price_spoken"] == "61 dollars and 37 cents"


def test_check_inventory_for_an_exempt_category_is_untouched(fake_bt):
    fake_bt.check = dict(CHECK, size=None)
    out = suggest.handle_check_inventory({"store": "yakima", "sku": "a", "category": "topical"}, dict(CTX))
    assert "needs_size" not in out and out["price_otd"] == 61.37


def test_check_inventory_out_of_stock_is_unchanged(fake_bt):
    fake_bt.check = {"in_stock": False}
    assert suggest.handle_check_inventory({"store": "yakima", "sku": "a"}, dict(CTX)) == {"in_stock": False}


# ── the path the phone agent and the text brain both take ────────────────────────────────────
def test_dispatch_keeps_the_gate_and_the_slot_wall_keeps_size_and_category(fake_bt):
    out = dispatch("suggest_products", {"store": "yakima", "category": "flower"}, dict(CTX))
    assert out["needs_size"] is True
    _assert_no_price(out)

    out = dispatch("suggest_products", {"store": "yakima", "category": "flower", "size": "3.5g"}, dict(CTX))
    assert out["picks"][0]["price_otd"] == PRICES["a"]

    fake_bt.check = dict(CHECK)
    out = dispatch("check_inventory", {"store": "yakima", "sku": "a"}, dict(CTX))
    assert out["needs_size"] is True
    _assert_no_price(out)
    out = dispatch(
        "check_inventory", {"store": "yakima", "sku": "a", "category": "flower", "size": "3.5g"}, dict(CTX)
    )
    assert out["price_otd"] == 61.37


def test_the_why_this_dollar_strip_keeps_the_rest_of_the_reason():
    assert suggest._why_without_dollars("On sale — save $9 · Dialed in for relaxed") == "Dialed in for relaxed"
    assert suggest._why_without_dollars("Dialed in for relaxed · On sale — save $9") == "Dialed in for relaxed"
    assert suggest._why_without_dollars("On sale — save $9") == ""
    assert suggest._why_without_dollars("Easy all-rounder") == "Easy all-rounder"
    assert suggest._why_without_dollars(None) is None


# ── the aroma slot reaches the budtender search body ─────────────────────────────────────────
def test_aroma_is_declared_with_the_five_scents_and_survives_the_slot_wall():
    props = C.TOOL_SPECS["suggest_products"]["parameters"]["properties"]
    assert props["aroma"]["enum"] == ["citrus", "earthy", "pine", "floral", "spicy"]
    kept = _sanitize_args("suggest_products", {"store": "yakima", "category": "flower", "aroma": "citrus"})
    assert kept["aroma"] == "citrus", "the slot wall dropped aroma"
    for junk in ("minty", "", "Citrus", None, 5):
        assert "aroma" not in _sanitize_args("suggest_products", {"category": "flower", "aroma": junk})


@pytest.mark.parametrize("aroma", C.AROMAS)
def test_aroma_reaches_the_search_body(fake_bt, aroma):
    dispatch("suggest_products", {"store": "yakima", "category": "flower", "size": "3.5g", "aroma": aroma}, dict(CTX))
    assert fake_bt.search_calls[-1]["slots"]["aroma"] == aroma


def test_no_aroma_means_no_aroma_slot(fake_bt):
    dispatch("suggest_products", {"store": "yakima", "category": "flower", "size": "3.5g"}, dict(CTX))
    assert "aroma" not in fake_bt.search_calls[-1]["slots"]


def test_the_aroma_question_names_exactly_the_five_scents():
    assert C.AROMA_QUESTION == "Any scent you're drawn to — citrus, earthy, pine, floral, or spicy?"
