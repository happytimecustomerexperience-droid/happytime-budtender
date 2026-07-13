"""
Unit tests for the shared suggestion engine — the ONE scoring brain both the
website/voice API and the in-store POS call.

The existing behavioural golden tests (test_no_leak.py) already guard the API
ordering; these cover the pieces that are NEW in the unified engine and are not
otherwise exercised: the `from_product` ORM→dict mapping, and the three POS
superset signals folded in (flavor affinity, THC-band fit, dual category lookup).
Pure-dict scoring runs without a database (SimpleTestCase); pair_for hits the ORM.
"""
from django.test import SimpleTestCase, TestCase, override_settings

from budtender import engine
from budtender.models import Product


def _ctx(**over):
    ctx = {
        "W": engine.W_ANON, "m_lo": 0.0, "span": 1.0, "mid": 30.0, "desired": None,
        "category": None, "price_sensitive": False, "recent_brands": set(), "recent_cats": set(),
    }
    ctx.update(over)
    return ctx


class FromProductMappingTests(SimpleTestCase):
    """The ORM adapter must feed the scorer the right values under the canonical
    keys — this is the join that keeps ORM and POS ranking identical."""

    def test_field_renames_and_types(self):
        p = Product(
            sku="X", location_slug="yakima", name="Blue Dream", brand="Acme",
            category="flower", subcategory="3.5g", strain="Blue Dream", strain_type="hybrid",
            dominant_terpene="limonene", thc_percent=22.0, price=30, price_was=35,
            quantity_on_hand=7, margin=18, margin_pct=0.6, price_z=0.5, bucket="profit", velocity=2,
        )
        feat = engine.from_product(p)
        self.assertEqual(feat["id"], "X")
        self.assertEqual(feat["terpene"], "limonene")   # dominant_terpene -> terpene
        self.assertEqual(feat["thc"], 22.0)             # thc_percent -> thc
        self.assertEqual(feat["qty"], 7)                # quantity_on_hand -> qty
        self.assertEqual(feat["margin"], 18.0)          # gross $ (server-only)
        self.assertEqual(feat["bucket"], "profit")
        self.assertEqual(feat["category"], feat["cat_key"])  # ORM has no separate cat_key


class SupersetSignalTests(SimpleTestCase):
    """The POS-side signals now folded into the one engine."""

    def test_flavor_affinity_folds_into_affinity(self):
        pf = {"flavor_affinity": {"citrus": 1.0}}
        matched = {"brand": "", "strain_type": "", "category": "", "cat_key": "",
                   "subcategory": "", "terpene": "", "flavors": ["citrus"]}
        unmatched = {**matched, "flavors": ["earthy"]}
        self.assertAlmostEqual(engine._affinity_score(matched, pf), 0.4)   # 0.4 * flavor sub-term
        self.assertEqual(engine._affinity_score(unmatched, pf), 0.0)

    def test_dual_category_lookup_takes_the_higher(self):
        # POS: raw category != canonical cat_key; the customer's affinity may be
        # keyed under either — the engine must not silently miss on a vocab mismatch.
        pf = {"category_affinity": {"vapes": 1.0}}
        feat = {"brand": "", "strain_type": "", "category": "vape-cartridges",
                "cat_key": "vapes", "subcategory": "", "terpene": "", "flavors": []}
        self.assertAlmostEqual(engine._affinity_score(feat, pf), 0.6)   # 0.6 * cat_key match

    def test_category_lookup_handles_api_and_pos_vocab_mismatch(self):
        pf = {"category_affinity": {"vape-cartridges": 1.0}}
        feat = {"brand": "", "strain_type": "", "category": "Vaporizer",
                "cat_key": "vapes", "subcategory": "", "terpene": "", "flavors": []}
        self.assertAlmostEqual(engine._affinity_score(feat, pf), 0.6)
        self.assertAlmostEqual(
            engine._recency_boost(feat, set(), {"vape-cartridges"}),
            0.05,
        )

    def test_thc_band_fit_is_an_additive_nudge(self):
        pf = {"thc_min": 15.0, "thc_max": 25.0}
        base = {"brand": "", "strain_type": "", "category": "", "cat_key": "",
                "subcategory": "", "terpene": "", "flavors": [], "margin": 0.0,
                "price": 30.0, "bucket": "core"}
        in_band = {**base, "thc": 20.0}
        no_thc = {**base, "thc": None}
        self.assertEqual(engine._thc_band_fit(in_band, pf), 1.0)
        self.assertEqual(engine._thc_band_fit(no_thc, pf), 0.0)
        # score_one folds it in additively (+0.12 * fit), holding everything else equal.
        diff = engine.score_one(in_band, pf, _ctx()) - engine.score_one(no_thc, pf, _ctx())
        self.assertAlmostEqual(diff, 0.12)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PairForTests(TestCase):
    """Pairing is now engine-owned; smoke it end-to-end on the ORM."""

    def test_pairs_a_cheaper_lighter_complement(self):
        anchor = Product.objects.create(
            sku="ANCHOR", location_slug="yakima", name="Big Flower", category="flower",
            price=40, cost=10, margin=30, quantity_on_hand=10, availability=True,
        )
        Product.objects.create(
            sku="PR", location_slug="yakima", name="House Pre-Roll", category="pre-rolls",
            price=8, cost=2, margin=6, quantity_on_hand=10, availability=True,
        )
        pair, reason, text, strength = engine.pair_for("yakima", anchor, None)
        self.assertIsNotNone(pair)
        self.assertEqual(pair.sku, "PR")
        self.assertTrue(text)

    def test_no_pair_when_nothing_cheaper_in_stock(self):
        anchor = Product.objects.create(
            sku="A2", location_slug="pullman", name="Cheap Flower", category="flower",
            price=10, cost=2, margin=8, quantity_on_hand=10, availability=True,
        )
        Product.objects.create(
            sku="PR2", location_slug="pullman", name="Pricey Pre-Roll", category="pre-rolls",
            price=9, cost=1, margin=8, quantity_on_hand=10, availability=True,
        )  # 9 > 50% of 10 -> gated out
        pair, reason, text, strength = engine.pair_for("pullman", anchor, None)
        self.assertIsNone(pair)
