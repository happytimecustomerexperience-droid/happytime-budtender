"""P5 #4 — cartridge entry directly from the entry_router (15-P5 §6 AC-1; §7.1).

The load-bearing invariant: a caller who OPENS with "I want a cart / a 510 / a vape pen" is
classified to ``category:"cartridge"`` UP FRONT — never buried under a ``concentrate`` sub-branch
(the export-#4 bug). A "disposable / dispo / AIO" opener also sets ``subcategory:"disposable"``;
a bare cartridge opener stays subcategory-less (budtender's facets pick reusable-vs-disposable).
Non-cartridge retail openers still classify their own category (no regression). Expected values
hand-authored (03-CONVENTIONS.md §5).
"""

from __future__ import annotations

import pytest

from voice import routing

# Bare cartridge openers → ("cartridge", None) — reusable-vs-disposable is budtender's facet call.
CARTRIDGE_OPENERS = [
    "I want a cart.",
    "Got any 510 carts?",
    "Looking for a vape pen.",
    "Do you have any vapes?",
    "I'm after a cartridge.",
    "I want a pod.",
]

# Disposable / all-in-one openers → ("cartridge", "disposable").
DISPOSABLE_OPENERS = [
    "A disposable vape pen, please.",
    "I want a dispo.",
    "Got any all-in-one carts?",
    "Looking for an AIO.",
]


@pytest.mark.parametrize("opener", CARTRIDGE_OPENERS)
def test_cartridge_openers_classify_cartridge_not_concentrate(opener):
    """AC-1: every cartridge opener → category 'cartridge', NEVER 'concentrate'; no subcategory."""
    category, subcategory = routing.classify_category(opener)
    assert category == "cartridge"
    assert category != "concentrate"
    assert subcategory is None


@pytest.mark.parametrize("opener", DISPOSABLE_OPENERS)
def test_disposable_openers_set_disposable_subcategory(opener):
    """AC-1: a disposable/AIO opener → category 'cartridge' + subcategory 'disposable'."""
    category, subcategory = routing.classify_category(opener)
    assert category == "cartridge"
    assert subcategory == "disposable"


def test_concentrate_opener_is_not_cartridge():
    """A dab/wax/rosin opener stays 'concentrate' — the cartridge guard doesn't over-reach."""
    category, _ = routing.classify_category("I'm looking for some live resin to dab.")
    assert category == "concentrate"


def test_non_cartridge_categories_classify_correctly():
    """No regression: edibles/flower/tincture each classify their own category."""
    assert routing.classify_category("I need some gummies.")[0] == "edible"
    assert routing.classify_category("Looking for an eighth of flower.")[0] == "flower"
    assert routing.classify_category("Got any tinctures?")[0] == "tincture"


def test_no_category_named_returns_none():
    """A retail opener that names no category → (None, None); budtender slot-fills it."""
    assert routing.classify_category("Something to help me relax tonight.") == (None, None)


def test_classify_full_object_cartridge_up_front():
    """AC-1: the structured ``classify`` output carries category 'cartridge' on a retail intent —
    the entry_router → budtender contract (§4.2). Bare cart → no subcategory key."""
    out = routing.classify("I want a cart.")
    assert out["intent"] == routing.INTENT_RETAIL
    assert out["category"] == "cartridge"
    assert "subcategory" not in out


def test_classify_full_object_disposable_subcategory():
    """A disposable opener carries both category 'cartridge' and subcategory 'disposable'."""
    out = routing.classify("I want a disposable vape pen.")
    assert out["category"] == "cartridge"
    assert out["subcategory"] == "disposable"


def test_classify_non_retail_carries_no_category():
    """A vendor/faq/escalation intent never carries a product category (specialist owns slots)."""
    vendor = routing.classify("I'm dropping off a delivery for the back.")
    assert vendor["intent"] == routing.INTENT_VENDOR
    assert "category" not in vendor
    faq = routing.classify("What time do you close?")
    assert "category" not in faq


def test_slot_builder_never_rewrites_cartridge_to_concentrate():
    """AC-1: the suggest_products arg builder forwards category 'cartridge' UNCHANGED; a cartridge
    alias (510/vape/disposable) canonicalizes to 'cartridge', never 'concentrate'."""
    from voice.tools import suggest

    for alias in ("cartridge", "cart", "510", "vape pen", "disposable", "aio", "pod"):
        slots = suggest._slots_from_args({"category": alias}, "yakima")
        assert slots["category"] == "cartridge"
        assert slots["category"] != "concentrate"
    # a genuine concentrate request stays concentrate (the guard doesn't over-reach).
    slots = suggest._slots_from_args({"category": "concentrate"}, "yakima")
    assert slots["category"] == "concentrate"
