"""Substitution behaviour.

The rule the owner set: when an emailed item is gone, widen to same category +
same weight, ANY brand — never leave a slot empty over a brand mismatch, and never
cross a size boundary (a 1g cart does not satisfy a 3.5g flower slot; crossing it
silently breaks the bundle's discount math).
"""
from django.test import TestCase, override_settings

from budtender.models import Product
from bundles import resolver
from bundles.catalog import get_bundle


def live(**kw):
    """A row shaped like `pos.catalog._normalize` output.

    Must carry EVERY key `_normalize` emits, including the ones only
    `pos.catalog.query` touches (`raw_category`, `strain`). A fixture that is
    missing a key doesn't fail loudly — it makes the code under test look like it
    works while the real filter path would KeyError in production.
    """
    d = dict(
        product_id="1", name="Blue Dream 3.5g", brand="Acme",
        raw_category="Flower", category="Flower", cat_key="flower", cat_label="Flower",
        strain="Blue Dream", strain_type="hybrid", terpene="myrcene",
        effects=["relaxed"], flavors=["berry"],
        thc=22.0, cbd=0.1, total_terpenes=2.0,
        price=25.0, price_was=0.0, qty=10,
        subcategory="3.5g", unit_grams=3.5, potency_mg=None,
        image="", img="", img_static=False, received_date=None, vendor="Acme Farms",
        velocity=1.0, margin_pct=0.5, price_z=0.1, bucket="core",
        ProductId=1, BatchId=9, SerialNo="S1", package_id="P1",
        UnitPrice=25.0, RecUnitPrice=25.0, ProductDesc="Blue Dream 3.5g",
        CannbisProduct="Yes",
    )
    d.update(kw)
    return d


def test_live_fixture_matches_normalize_output():
    """Guard the guard: if `_normalize` grows a key, this fixture must too."""
    from pos.catalog import _normalize
    real = _normalize({"ProductId": 1, "ProductDescription": "X", "ProductCategory": "Flower",
                       "UnitPrice": 1, "TotalAvailable": 1}, {})
    missing = set(real) - set(live())
    assert not missing, f"live() fixture is missing keys _normalize emits: {sorted(missing)}"


def db_product(**kw):
    d = dict(sku="A", product_id="1", location_slug="yakima", name="Blue Dream 3.5g",
             brand="Acme", category="flower", subcategory="3.5g", unit_weight=3.5,
             strain_type="hybrid", dominant_terpene="myrcene", effects=["relaxed"],
             flavors=["berry"], price=25, cost=10, margin=15, quantity_on_hand=10)
    d.update(kw)
    return Product.objects.create(**d)


@override_settings(BUNDLE_MIN_STOCK=2)
class InStockTests(TestCase):
    def test_more_than_one_is_proposable_exactly_one_is_not(self):
        # "if >1 is in stock we can propose it"
        self.assertTrue(resolver.in_stock(live(qty=2)))
        self.assertFalse(resolver.in_stock(live(qty=1)))
        self.assertFalse(resolver.in_stock(live(qty=0)))


class SubstitutionGateTests(TestCase):
    def setUp(self):
        self.bundle = get_bundle("roll-relax")
        self.flower_slot = self.bundle.slots[0]

    def test_category_is_a_hard_gate(self):
        target = {"cat_key": "flower", "subcategory": "3.5g", "price": 25.0}
        inv = [live(product_id="2", cat_key="vapes", subcategory="1g", name="Cart")]
        self.assertEqual(resolver.candidates_for(inv, target), [])

    def test_size_is_a_hard_gate_for_flower(self):
        target = {"cat_key": "flower", "subcategory": "3.5g", "unit_grams": 3.5, "price": 25.0}
        inv = [live(product_id="2", subcategory="28g", unit_grams=28.0, name="Ounce")]
        self.assertEqual(resolver.candidates_for(inv, target, slot=self.flower_slot), [])

    def test_brand_is_not_a_gate_we_widen_past_it(self):
        target = {"cat_key": "flower", "subcategory": "3.5g", "unit_grams": 3.5,
                  "brand": "Acme", "price": 25.0}
        other = live(product_id="2", brand="Nother Brand", name="Other 3.5g")
        got = resolver.candidates_for([other], target, slot=self.flower_slot)
        self.assertEqual([p["product_id"] for p in got], ["2"])

    def test_same_brand_outranks_a_different_brand_all_else_equal(self):
        target = {"cat_key": "flower", "subcategory": "3.5g", "unit_grams": 3.5,
                  "brand": "Acme", "price": 25.0}
        inv = [live(product_id="2", brand="Zzz", name="Z"),
               live(product_id="3", brand="Acme", name="A")]
        got = resolver.candidates_for(inv, target, slot=self.flower_slot)
        self.assertEqual(got[0]["product_id"], "3")

    def test_closer_price_outranks_a_far_one(self):
        target = {"cat_key": "flower", "subcategory": "3.5g", "unit_grams": 3.5, "price": 25.0}
        inv = [live(product_id="2", price=60.0, brand="X"),
               live(product_id="3", price=26.0, brand="Y")]
        got = resolver.candidates_for(inv, target, slot=self.flower_slot)
        self.assertEqual(got[0]["product_id"], "3")

    def test_out_of_stock_rows_are_never_candidates(self):
        target = {"cat_key": "flower", "subcategory": "3.5g", "unit_grams": 3.5, "price": 25.0}
        inv = [live(product_id="2", qty=0), live(product_id="3", qty=1)]
        self.assertEqual(resolver.candidates_for(inv, target, slot=self.flower_slot), [])

    def test_already_used_products_are_excluded(self):
        target = {"cat_key": "flower", "subcategory": "3.5g", "unit_grams": 3.5, "price": 25.0}
        inv = [live(product_id="2")]
        self.assertEqual(resolver.candidates_for(inv, target, slot=self.flower_slot,
                                                 exclude={"2"}), [])


class ResolveTests(TestCase):
    def setUp(self):
        self.bundle = get_bundle("roll-relax")

    def _inventory(self):
        return [
            live(product_id="1", name="Blue Dream 3.5g", price=25.0),
            live(product_id="2", name="OG 3.5g", brand="Other", price=27.0),
            live(product_id="10", cat_key="pre-rolls", subcategory="1pk", name="PR One",
                 unit_grams=1.0, price=8.0),
            live(product_id="11", cat_key="pre-rolls", subcategory="1pk", name="PR Two",
                 unit_grams=1.0, price=9.0),
            live(product_id="20", cat_key="edibles", subcategory="10pk", name="Gummies",
                 unit_grams=None, price=15.0),
        ]

    def test_all_in_stock_resolves_clean(self):
        db_product()
        out = resolver.resolve(self.bundle, "yakima",
                               [("1", 1), ("10", 2), ("20", 1)],
                               inventory=self._inventory())
        self.assertTrue(out["complete"])
        self.assertEqual(out["substitutions"], 0)
        self.assertEqual(out["missing"], 0)
        self.assertEqual([line.status for line in out["lines"]], [resolver.OK] * 3)

    def test_sold_out_item_is_substituted_from_the_same_category_and_size(self):
        db_product()   # advertised identity for sku "A"/product_id "1"
        inv = [p for p in self._inventory() if p["product_id"] != "1"]
        out = resolver.resolve(self.bundle, "yakima",
                               [("1", 1), ("10", 2), ("20", 1)], inventory=inv)
        first = out["lines"][0]
        self.assertEqual(first.status, resolver.SUBSTITUTED)
        self.assertEqual(first.product["product_id"], "2")
        self.assertEqual(first.product["size"], "3.5g")
        self.assertEqual(out["substitutions"], 1)

    def test_no_match_leaves_an_explicit_open_slot(self):
        db_product()
        # Only pre-rolls and edibles on the floor — nothing can fill the flower slot.
        inv = [p for p in self._inventory() if p["cat_key"] != "flower"]
        out = resolver.resolve(self.bundle, "yakima",
                               [("1", 1), ("10", 2), ("20", 1)], inventory=inv)
        self.assertEqual(out["lines"][0].status, resolver.UNAVAILABLE)
        self.assertFalse(out["complete"])
        self.assertGreaterEqual(out["missing"], 1)

    def test_discount_math(self):
        db_product()
        out = resolver.resolve(self.bundle, "yakima",
                               [("1", 1), ("10", 2), ("20", 1)],
                               inventory=self._inventory())
        # 25 + (8*2) + 15 = 56 ; 20% off
        self.assertEqual(out["subtotal"], 56.0)
        self.assertEqual(out["discount"], 11.2)
        self.assertEqual(out["total"], 44.8)

    def test_uncovered_slots_are_surfaced_not_silently_dropped(self):
        db_product()
        out = resolver.resolve(self.bundle, "yakima", [("1", 1)],
                               inventory=self._inventory())
        slots = {line.slot.key for line in out["lines"] if line.slot}
        self.assertIn("preroll", slots)
        self.assertIn("edible", slots)

    def test_empty_inventory_degrades_without_raising(self):
        out = resolver.resolve(self.bundle, "yakima", [("1", 1)], inventory=[])
        self.assertFalse(out["inventory_live"])
        self.assertFalse(out["complete"])

    def test_same_product_is_not_used_twice(self):
        db_product()
        inv = [live(product_id="1", name="Only 3.5g", price=25.0),
               live(product_id="10", cat_key="pre-rolls", subcategory="1pk",
                    name="PR", unit_grams=1.0, price=8.0)]
        out = resolver.resolve(self.bundle, "yakima", [("999", 1), ("998", 1)], inventory=inv)
        chosen = [line.product["product_id"] for line in out["lines"] if line.product]
        self.assertEqual(len(chosen), len(set(chosen)))


class PublicProjectionTests(TestCase):
    def test_staff_only_signals_never_survive_projection(self):
        pub = resolver._public(live())
        for leaked in ("margin_pct", "velocity", "price_z", "bucket", "BatchId",
                       "SerialNo", "package_id", "UnitPrice", "RecUnitPrice",
                       "ProductId", "CannbisProduct", "vendor", "received_date"):
            self.assertNotIn(leaked, pub, f"{leaked} leaked to the public page")

    def test_projection_keeps_what_the_page_needs(self):
        pub = resolver._public(live())
        for needed in ("product_id", "name", "brand", "price", "qty", "size"):
            self.assertIn(needed, pub)
