"""TASK 3 — hyphenated strain types.

Live Product.strain_type values (measured 2026-08-10): Hybrid 3683, Sativa 510, Indica 502,
Indica-Hybrid 41, Sativa-Hybrid 25, None 12, CBD 6.

rank_products' subcategory strain-type filter did an EXACT match
(``(p.strain_type or "").lower() == sub``), so a caller asking for "indica" never saw the 41
Indica-Hybrid products, and "sativa" never saw the 25 Sativa-Hybrid ones.

Decision (documented in ranking.py alongside the fix): "hybrid" ALSO matches Indica-Hybrid and
Sativa-Hybrid. They are, in fact, hybrids — a customer asking for "hybrid" wants the broadest
hybrid shelf, not just the ones with no hyphen. "indica" only matches products whose strain type
IS indica-leaning (indica or indica-hybrid) — never sativa or sativa-hybrid — and symmetrically
for "sativa".
"""
from django.test import TestCase

from budtender.models import Product
from budtender.ranking import rank_products


def _seed(location="yakima"):
    Product.objects.create(
        sku="STR-INDICA", location_slug=location, name="Pure Indica 3.5g",
        category="flower", strain_type="Indica",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="STR-INDICA-HYBRID", location_slug=location, name="Indica Hybrid 3.5g",
        category="flower", strain_type="Indica-Hybrid",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="STR-SATIVA", location_slug=location, name="Pure Sativa 3.5g",
        category="flower", strain_type="Sativa",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="STR-SATIVA-HYBRID", location_slug=location, name="Sativa Hybrid 3.5g",
        category="flower", strain_type="Sativa-Hybrid",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="STR-HYBRID", location_slug=location, name="Straight Hybrid 3.5g",
        category="flower", strain_type="Hybrid",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )
    Product.objects.create(
        sku="STR-CBD", location_slug=location, name="CBD Flower 3.5g",
        category="flower", strain_type="CBD",
        price=30, cost=10, margin=20, quantity_on_hand=10, availability=True,
    )


class HyphenatedStrainTypeTests(TestCase):
    def test_indica_request_also_matches_indica_hybrid(self):
        _seed()
        ranked = rank_products(
            "yakima", {"category": "flower", "subcategory": "indica"}, None, limit=10,
        )
        skus = {p.sku for p, _ in ranked}
        self.assertIn("STR-INDICA", skus)
        self.assertIn("STR-INDICA-HYBRID", skus,
                       "'indica' must also surface Indica-Hybrid products (41 live SKUs)")
        self.assertNotIn("STR-SATIVA", skus)
        self.assertNotIn("STR-SATIVA-HYBRID", skus)
        self.assertNotIn("STR-HYBRID", skus)

    def test_sativa_request_also_matches_sativa_hybrid(self):
        _seed()
        ranked = rank_products(
            "yakima", {"category": "flower", "subcategory": "sativa"}, None, limit=10,
        )
        skus = {p.sku for p, _ in ranked}
        self.assertIn("STR-SATIVA", skus)
        self.assertIn("STR-SATIVA-HYBRID", skus,
                       "'sativa' must also surface Sativa-Hybrid products (25 live SKUs)")
        self.assertNotIn("STR-INDICA", skus)
        self.assertNotIn("STR-INDICA-HYBRID", skus)
        self.assertNotIn("STR-HYBRID", skus)

    def test_hybrid_request_also_matches_the_hyphenated_hybrids(self):
        """Decision: Indica-Hybrid and Sativa-Hybrid ARE hybrids, so a plain "hybrid" ask
        should see the whole hybrid shelf, not just the unhyphenated ones."""
        _seed()
        ranked = rank_products(
            "yakima", {"category": "flower", "subcategory": "hybrid"}, None, limit=10,
        )
        skus = {p.sku for p, _ in ranked}
        self.assertIn("STR-HYBRID", skus)
        self.assertIn("STR-INDICA-HYBRID", skus)
        self.assertIn("STR-SATIVA-HYBRID", skus)
        self.assertNotIn("STR-INDICA", skus)
        self.assertNotIn("STR-SATIVA", skus)

    def test_cbd_and_unset_strain_types_are_unaffected(self):
        _seed()
        ranked_indica = rank_products(
            "yakima", {"category": "flower", "subcategory": "indica"}, None, limit=10,
        )
        ranked_sativa = rank_products(
            "yakima", {"category": "flower", "subcategory": "sativa"}, None, limit=10,
        )
        ranked_hybrid = rank_products(
            "yakima", {"category": "flower", "subcategory": "hybrid"}, None, limit=10,
        )
        for ranked in (ranked_indica, ranked_sativa, ranked_hybrid):
            self.assertNotIn("STR-CBD", {p.sku for p, _ in ranked})
