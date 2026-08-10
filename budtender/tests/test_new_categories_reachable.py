"""TASK 1 — live Dutchie inventory (measured 2026-08-10) has 11 in-stock categories, but
CATEGORY_BY_SLOTKEY only mapped 6 of them (flower/concentrates/vape-cartridges/edibles/
tinctures/pre-rolls). 97 in-stock products across topicals/capsules/mints/blunt/infused-blunt
could NEVER be recommended by voice or chat, because a category slot key that isn't in this
map falls through to the raw (unmapped) slot value and never matches any Product.category row.

This test seeds one product per newly-reachable category and asks rank_products for it by the
NEW slot key — proving a caller can now actually get that category back.
"""
from django.test import TestCase

from budtender.models import Product
from budtender.ranking import rank_products


def _seed(location="yakima"):
    Product.objects.create(
        sku="TOP-1", location_slug=location, name="CBD Relief Balm 100mg",
        category="topicals", price=25, cost=10, margin=15,
        quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="CAP-1", location_slug=location, name="10mg THC Capsules 10ct",
        category="capsules", price=20, cost=8, margin=12,
        quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="MINT-1", location_slug=location, name="Peppermint THC Mints 10ct",
        category="mints", price=15, cost=6, margin=9,
        quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="BLUNT-1", location_slug=location, name="Grape Blunt 2g",
        category="blunt", price=18, cost=7, margin=11,
        quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="IBLUNT-1", location_slug=location, name="Infused Blunt Diamond 2g",
        category="infused-blunt", price=28, cost=11, margin=17,
        quantity_on_hand=10, availability=True,
    )


class NewCategoriesReachableTests(TestCase):
    def test_topical_slot_key_reaches_topicals_category(self):
        _seed()
        ranked = rank_products("yakima", {"category": "topical"}, None, limit=10)
        self.assertIn("TOP-1", {p.sku for p, _ in ranked})

    def test_capsule_slot_key_reaches_capsules_category(self):
        _seed()
        ranked = rank_products("yakima", {"category": "capsule"}, None, limit=10)
        self.assertIn("CAP-1", {p.sku for p, _ in ranked})

    def test_mint_slot_key_reaches_mints_category(self):
        _seed()
        ranked = rank_products("yakima", {"category": "mint"}, None, limit=10)
        self.assertIn("MINT-1", {p.sku for p, _ in ranked})

    def test_blunt_slot_key_reaches_blunt_category(self):
        _seed()
        ranked = rank_products("yakima", {"category": "blunt"}, None, limit=10)
        self.assertIn("BLUNT-1", {p.sku for p, _ in ranked})

    def test_infused_blunt_slot_key_reaches_infused_blunt_category(self):
        _seed()
        ranked = rank_products("yakima", {"category": "infused-blunt"}, None, limit=10)
        self.assertIn("IBLUNT-1", {p.sku for p, _ in ranked})
