"""What a product card states must be what the search actually used (2026-10-09 audit of the website chat).

- a bounded $100-$150 band is a hard price gate (it used to switch to "premium" and return $200 items)
- the card's price/stock come from the live sales-floor row the ranker gated on, not the stale table
- the card shows the pack ("10pk") for packs, so a 5-pack and a 10-pack are told apart on screen"""
import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from budtender.live_stock import StockMap
from budtender.models import Product
from budtender.ranking import rank_products
from budtender.serializers import public_product

TOKEN = "backend-token"


def _p(sku, name, category="flower", price=30, **kw):
    return Product.objects.create(sku=sku, product_id=f"P-{sku}", location_slug="yakima", name=name,
                                  category=category, price=price, cost=8, margin=12, quantity_on_hand=10,
                                  availability=True, **kw)


class BoundedPremiumBandTests(TestCase):
    def setUp(self):
        _p("OZ120", "Ounce A 28g", price=120, unit_weight=28)
        _p("OZ200", "Ounce B 28g", price=200, unit_weight=28)

    def test_a_100_to_150_band_never_returns_a_200_item(self):
        skus = {p.sku for p, _ in rank_products("yakima", {"category": "flower", "size": "28g", "price_min": 100,
                                                           "price_max": 150}, None, limit=5)}
        self.assertEqual(skus, {"OZ120"})

    def test_100_and_up_is_still_premium_and_open_ended(self):
        skus = [p.sku for p, _ in rank_products("yakima", {"category": "flower", "size": "28g", "price_min": 100},
                                                None, limit=5)]
        self.assertEqual(skus[0], "OZ200")  # priciest first, nothing above the floor excluded


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class LiveCardTests(TestCase):
    def test_the_search_card_prints_the_live_price_and_stock(self):
        _p("A", "Blue Dream 3.5g", price=40, unit_weight=3.5)
        live = StockMap("yakima", source="live", by_sku={"A": {"price": 32.0, "quantity_on_hand": 7}})
        with patch("budtender.live_stock.stock_map", return_value=live), \
             patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.fire", lambda *a, **k: False):
            r = Client().post("/api/v1/products/search/", data=json.dumps({"slots": {"store": "yakima",
                              "category": "flower"}}), content_type="application/json",
                              HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        card = r.json()["results"][0]
        self.assertEqual(card["price"], 32.0)  # the table says 40; the floor says 32


class PackSizeOnCardTests(TestCase):
    def test_packs_show_the_pack_and_a_single_keeps_its_weight(self):
        self.assertEqual(public_product(_p("E10", "Wyld Gummies 10pk", category="edibles"))["size"], "10pk")
        self.assertEqual(public_product(_p("E5", "Wyld Gummies 5pk", category="edibles"))["size"], "5pk")
        self.assertEqual(public_product(_p("R5", "Joints 5pk", category="pre-rolls", unit_weight=2.5))["size"], "5pk")
        self.assertEqual(public_product(_p("R1", "Joint 0.5g", category="pre-rolls", unit_weight=0.5))["size"], "0.5g")
        self.assertIsNone(public_product(_p("E0", "Chocolate Bar", category="edibles"))["size"])
