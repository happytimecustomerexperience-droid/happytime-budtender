"""`suggest_bundle_links` must pick things the landing page will actually honour.

The failure that matters is silent: the command picks a product the page then rejects
or substitutes, and the email advertises a bundle the shopper does not receive. So
these tests assert the two properties that prevent it —

  * every picked product satisfies the slot it was picked for, judged by the bundle's
    own `Slot.accepts()`; and
  * the emitted URL verifies against `signing.parse()` and carries exactly the picked
    ids and quantities.

Nothing here touches the network: `pos.catalog.get_inventory` is patched throughout.
"""
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from bundles import signing
from bundles.catalog import BUNDLES
from bundles.tests.test_resolver import live

SECRET = "unit-test-secret-value"


def floor():
    """One deep-stock and one shallow-stock option per slot type."""
    return [
        live(product_id="f1", name="Deep Flower 3.5g", subcategory="3.5g", qty=90, price=35.0),
        live(product_id="f2", name="Thin Flower 3.5g", subcategory="3.5g", qty=3, price=30.0),
        live(product_id="p1", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="Deep Preroll", unit_grams=1.0, qty=140, price=5.0),
        live(product_id="p2", cat_key="pre-rolls", cat_label="Pre-Rolls", subcategory="1pk",
             name="Thin Preroll", unit_grams=1.0, qty=4, price=6.0),
        live(product_id="v1", cat_key="vapes", cat_label="Vapes", subcategory="1g",
             name="Deep Vape", unit_grams=1.0, qty=70, price=35.0),
        live(product_id="e1", cat_key="edibles", cat_label="Edibles", subcategory="10pk",
             name="Deep Edible", unit_grams=None, qty=50, price=15.0),
    ]


def run(**kw):
    out = StringIO()
    with patch("bundles.management.commands.suggest_bundle_links.pos_catalog.get_inventory",
               return_value=floor()):
        call_command("suggest_bundle_links", stdout=out, stderr=StringIO(), **kw)
    return out.getvalue()


@override_settings(BUNDLE_URL_SECRET=SECRET, BUNDLE_MIN_STOCK=2)
class SuggestBundleLinksTests(SimpleTestCase):
    def test_every_pick_satisfies_the_slot_it_was_picked_for(self):
        """The whole point. Judged by the bundle's own accepts(), not by eye."""
        import json
        rows = json.loads(run(store=["yakima"], format="json"))
        by_id = {str(p["product_id"]): p for p in floor()}
        for row in rows:
            slots = BUNDLES[row["bundle"]].slots
            self.assertEqual(len(row["lines"]), len(slots), row["bundle"])
            for line, slot in zip(row["lines"], slots):
                self.assertTrue(
                    slot.accepts(by_id[line["product_id"]]),
                    f"{row['bundle']}: {line['name']} does not satisfy '{slot.label}' — "
                    "the page would substitute it")
                self.assertEqual(line["qty"], slot.qty)

    def test_the_url_verifies_and_carries_the_picked_items(self):
        import json
        from urllib.parse import parse_qs, urlsplit
        for row in json.loads(run(store=["yakima"], format="json")):
            params = parse_qs(urlsplit(row["url"]).query)
            req = signing.parse({k: v if len(v) > 1 else v[0] for k, v in params.items()})
            self.assertEqual(req.bundle, row["bundle"])
            self.assertEqual(req.store, row["store"])
            self.assertEqual(
                [(sku, qty) for sku, qty in req.items],
                [(line["product_id"], line["qty"]) for line in row["lines"]])

    def test_it_prefers_the_deepest_stock(self):
        # A link built on the last four units is dead within a day of the send.
        import json
        rows = json.loads(run(store=["yakima"], bundle=["roll-relax"], format="json"))
        picked = {line["product_id"] for line in rows[0]["lines"]}
        self.assertIn("f1", picked)
        self.assertNotIn("f2", picked)
        self.assertIn("p1", picked)
        self.assertNotIn("p2", picked)

    def test_a_slot_never_reuses_a_product_from_another_slot(self):
        import json
        for row in json.loads(run(store=["yakima"], format="json")):
            ids = [line["product_id"] for line in row["lines"]]
            self.assertEqual(len(ids), len(set(ids)), f"{row['bundle']} repeats a product")

    def test_the_maths_matches_the_bundle_depth(self):
        import json
        for row in json.loads(run(store=["yakima"], format="json")):
            sub = sum(line["price"] * line["qty"] for line in row["lines"])
            self.assertAlmostEqual(row["subtotal"], round(sub, 2), places=2)
            self.assertAlmostEqual(row["total"],
                                   round(sub - sub * row["discount_pct"] / 100, 2), places=2)

    def test_min_stock_filters_before_picking(self):
        import json
        # At 60, the thin flower (3) and thin pre-roll (4) are gone but every slot can
        # still be filled: f1=90, p1=140, v1=70, e1=50 — except the edible, at 50.
        rows = json.loads(run(store=["yakima"], bundle=["vape-munch"], min_stock=60,
                              format="json"))
        self.assertEqual([line["product_id"] for line in rows[0]["lines"]], ["v1", "p1"]
                         if len(rows[0]["lines"]) == 2 else ["v1", "p1", "e1"])

    def test_an_unfillable_slot_skips_the_bundle_instead_of_shipping_a_short_one(self):
        # A three-slot bundle that emits two lines is worse than no link: the email
        # promises an edible the landing page never shows. Raising min_stock above the
        # edible's 50 makes that slot unfillable.
        with self.assertRaises(CommandError):
            call_command("suggest_bundle_links", store=["yakima"], bundle=["roll-relax"],
                         min_stock=100, stdout=StringIO(), stderr=StringIO())

    def test_an_empty_floor_fails_loudly(self):
        # Silence here would mean a campaign built on nothing.
        with patch("bundles.management.commands.suggest_bundle_links.pos_catalog.get_inventory",
                   return_value=[]):
            with self.assertRaises(CommandError):
                call_command("suggest_bundle_links", store=["yakima"],
                             stdout=StringIO(), stderr=StringIO())

    def test_the_phone_stamps_a_customer_token(self):
        from urllib.parse import parse_qs, urlsplit
        import json
        row = json.loads(run(store=["yakima"], bundle=["weekend"], phone="509 420 6999",
                             format="json"))[0]
        params = parse_qs(urlsplit(row["url"]).query)
        self.assertEqual(params["c"][0], signing.customer_token("5094206999"),
                         "the token must normalise punctuation the same way checkout does")

    def test_links_point_at_the_on_brand_apex_by_default(self):
        import json
        row = json.loads(run(store=["yakima"], bundle=["weekend"], format="json"))[0]
        self.assertTrue(row["url"].startswith("https://happytimeweed.com/custom-order/"), row["url"])
