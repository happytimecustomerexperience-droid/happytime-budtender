"""
`category_blocklist` — the voice slot for "not a concentrate" / "anything but
edibles" — must be a HARD filter in the real ranking engine, the same way
`doh_only` and `subcategory` are (budtender/ranking.py::rank_products).

Before this test existed, budtender never read the slot at all: only the
voice test suite's FakeClient implemented the filter, so production callers
who said "not a concentrate" could still be offered one.
"""
from django.test import TestCase

from budtender.models import Product
from budtender.ranking import rank_products


def _seed(location="yakima"):
    Product.objects.create(
        sku="FLOWER-1", location_slug=location, name="Blue Dream", category="flower",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="CONC-1", location_slug=location, name="Live Resin", category="concentrates",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="VAPE-1", location_slug=location, name="Cart 1g", category="vape-cartridges",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="EDIBLE-1", location_slug=location, name="Gummies", category="edibles",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )


class CategoryBlocklistHardFilterTests(TestCase):
    def test_blocklisted_category_never_comes_back(self):
        _seed()
        ranked = rank_products(
            "yakima", {"category_blocklist": ["concentrate"]}, None, limit=10,
        )
        cats = {p.category for p, _ in ranked}
        self.assertNotIn("concentrates", cats)
        self.assertIn("flower", cats)  # sanity: filter isn't dropping everything

    def test_blocklist_accepts_canonical_category_names_too(self):
        _seed()
        ranked = rank_products(
            "yakima", {"category_blocklist": ["edibles"]}, None, limit=10,
        )
        cats = {p.category for p, _ in ranked}
        self.assertNotIn("edibles", cats)

    def test_blocklist_ignores_unknown_garbage_entries(self):
        _seed()
        ranked = rank_products(
            "yakima", {"category_blocklist": ["not-a-real-category", ""]}, None, limit=10,
        )
        cats = {p.category for p, _ in ranked}
        # Garbage entries must not filter everything out.
        self.assertEqual(cats, {"flower", "concentrates", "vape-cartridges", "edibles"})

    def test_empty_blocklist_changes_nothing(self):
        _seed()
        baseline = rank_products("yakima", {}, None, limit=10)
        with_empty = rank_products("yakima", {"category_blocklist": []}, None, limit=10)
        self.assertEqual(
            {p.sku for p, _ in baseline},
            {p.sku for p, _ in with_empty},
        )

    def test_absent_blocklist_changes_nothing(self):
        _seed()
        baseline = rank_products("yakima", {}, None, limit=10)
        cats = {p.category for p, _ in baseline}
        self.assertEqual(cats, {"flower", "concentrates", "vape-cartridges", "edibles"})

    def test_blocklist_covering_every_category_returns_empty_not_off_spec(self):
        _seed()
        ranked = rank_products(
            "yakima",
            {"category_blocklist": ["flower", "concentrate", "cartridge", "edible"]},
            None,
            limit=10,
        )
        self.assertEqual(ranked, [])
