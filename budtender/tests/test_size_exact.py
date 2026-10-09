"""A requested size is honoured exactly, whatever words it arrives in (owner, 2026-10-09: the chat
offered a "5pk" edible and the click returned 10-packs).

Before: `_size_match` read an unrecognised size ("5 pack", "5-pack", "large") as "no filter", and
`rank_products` filled a missing pack/dose with every other size. Now a readable size is normalised
to the canonical value the sizes endpoint emits, an unreadable one matches nothing, and only a gram
weight may borrow its nearest neighbour (the documented flower fallback)."""
from django.test import TestCase

from budtender.models import Product
from budtender.ranking import normalize_size, rank_products


def _p(sku, name, category="edibles", **kw):
    return Product.objects.create(
        sku=sku, location_slug="yakima", name=name, category=category, price=kw.pop("price", 20), cost=8,
        margin=12, quantity_on_hand=10, availability=True, **kw)


def _skus(slots):
    return sorted(p.sku for p, _ in rank_products("yakima", slots, None, limit=10))


class NormalizeSizeTests(TestCase):
    def test_spellings_map_to_the_canonical_value(self):
        for raw, want in [("5 pack", "5pk"), ("5-pack", "5pk"), ("5 PK", "5pk"), ("5pk", "5pk"), ("10 ct", "10pk"),
                          ("an eighth", "3.5g"), ("3.5 g", "3.5g"), ("1 gram", "1g"), ("10 mg", "10mg"),
                          ("single", "single"), ("any", "any")]:
            self.assertEqual(normalize_size(raw), want, raw)

    def test_unreadable_is_none(self):
        for raw in ("large", "five pack", "", None, "5-pack of 10mg", "0 pack"):
            self.assertIsNone(normalize_size(raw), raw)


class ExactPackTests(TestCase):
    def setUp(self):
        _p("G5", "Wyld Raspberry Gummies 5pk")
        _p("G10", "Wyld Raspberry Gummies 10pk")
        _p("G20", "Camino Gummies 20pk")

    def test_every_spelling_of_five_pack_returns_only_five_packs(self):
        for size in ("5pk", "5 pack", "5-pack", "5 pk"):
            self.assertEqual(_skus({"category": "edible", "size": size}), ["G5"], size)

    def test_a_pack_count_the_shelf_lacks_is_an_honest_empty(self):
        self.assertEqual(_skus({"category": "edible", "size": "2pk"}), [])  # never 5/10/20-packs instead

    def test_an_unreadable_size_matches_nothing_rather_than_everything(self):
        self.assertEqual(_skus({"category": "edible", "size": "large"}), [])

    def test_no_size_still_returns_every_pack(self):
        self.assertEqual(_skus({"category": "edible"}), ["G10", "G20", "G5"])


class ExactDoseTests(TestCase):
    def test_a_missing_dose_never_fills_with_other_doses(self):
        _p("D100", "Big Bar 100mg", potency_mg=100)
        _p("D5", "Mint 5mg", potency_mg=5)
        self.assertEqual(_skus({"category": "edible", "size": "10mg"}), [])
        self.assertEqual(_skus({"category": "edible", "size": "5 mg"}), ["D5"])

    def test_a_dose_written_only_in_the_name_still_matches(self):
        _p("N10", "Gummy 10mg")  # no stored potency
        _p("N50", "Gummy 50mg")
        self.assertEqual(_skus({"category": "edible", "size": "10mg"}), ["N10"])


class GramFallbackTests(TestCase):
    def test_the_documented_nearest_weight_fallback_still_applies_to_grams(self):
        _p("F7", "Blue Dream 7g", category="flower", unit_weight=7)
        self.assertEqual(_skus({"category": "flower", "size": "eighth"}), ["F7"])  # 7g is 2x 3.5g: in window
        _p("F28", "Big Ounce 28g", category="flower", unit_weight=28)
        self.assertEqual(_skus({"category": "flower", "size": "1g"}), [])  # nothing within [0.5g, 2g]
