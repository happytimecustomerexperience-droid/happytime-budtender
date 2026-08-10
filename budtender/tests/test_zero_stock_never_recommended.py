"""
Proves `quantity_on_hand == 0` (and below-MIN_STOCK) is genuinely impossible to
recommend through the REAL path — the actual HTTP endpoint voice/chat call
(`POST /api/v1/products/search/` → ProductSearchView → rank_products), not a
mocked ranking function. Covers both stock sources rank_products can be gating
on: the DB fallback (no live pull) and a real live_stock pull.
"""
import json
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from budtender import live_stock, views
from budtender.engine import MIN_STOCK
from budtender.models import Product

LOC = "yakima"
TOKEN = "test-token"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _product(**kw):
    defaults = dict(
        location_slug=LOC, slug="p", name="Product", brand="Acme", category="flower",
        price=30, cost=10, margin=20, availability=True,
    )
    defaults.update(kw)
    return Product.objects.create(**defaults)


@override_settings(CACHES=CACHES_LOCMEM, HHT_BACKEND_TOKEN=TOKEN)
class ZeroStockNeverRecommendedTests(TestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        # A fresh test DB has no SyncState row, so inventory_is_stale() is always
        # True and the view would try to fire a real Celery task at an unreachable
        # broker. Not what this test is about — same sidestep test_product_search
        # _contract.py uses (mocking inventory_is_stale), just via the real HTTP path.
        patcher = mock.patch.object(views, "inventory_is_stale", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _search(self, **slots):
        resp = self.client.post(
            "/api/v1/products/search/",
            data=json.dumps({"slots": {"store": LOC, **slots}, "limit": 10}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        )
        self.assertEqual(resp.status_code, 200)
        return resp.json()["results"]

    def test_zero_stock_db_row_never_appears_via_the_real_http_endpoint(self):
        """No live pull available (source='db') → rank_products falls back to the
        table's own availability gate. A zero-stock row must never surface."""
        _product(sku="SOLD-OUT", quantity_on_hand=0)
        _product(sku="IN-STOCK", quantity_on_hand=MIN_STOCK + 5)
        skus = {r["sku"] for r in self._search(category="flower")}
        self.assertNotIn("SOLD-OUT", skus)
        self.assertIn("IN-STOCK", skus)

    def test_below_min_stock_db_row_never_appears_either(self):
        """Not just zero — anything under MIN_STOCK is equally unsellable."""
        _product(sku="ALMOST-GONE", quantity_on_hand=MIN_STOCK - 1)
        _product(sku="IN-STOCK", quantity_on_hand=MIN_STOCK + 5)
        skus = {r["sku"] for r in self._search(category="flower")}
        self.assertNotIn("ALMOST-GONE", skus)
        self.assertIn("IN-STOCK", skus)

    def test_zero_live_stock_overrides_a_stale_table_that_still_says_in_stock(self):
        """The real production failure this whole chain guards against: the table
        (refreshed on a ~10-min beat) still shows stock, but the LIVE sales-floor
        pull says zero — the live number must win and the item must not appear."""
        p = _product(sku="JUST-SOLD", quantity_on_hand=20)  # table is stale/optimistic
        live_stock.prime(LOC, [
            {"sku": p.sku, "product_id": p.product_id, "name": p.name, "brand": p.brand,
             "category": p.category, "price": 30.0, "price_was": None,
             "quantity_on_hand": 0.0},
        ])
        skus = {r["sku"] for r in self._search(category="flower")}
        self.assertNotIn("JUST-SOLD", skus)

    def test_live_stock_below_min_stock_also_excludes_it(self):
        p = _product(sku="THIN-STOCK", quantity_on_hand=50)
        live_stock.prime(LOC, [
            {"sku": p.sku, "product_id": p.product_id, "name": p.name, "brand": p.brand,
             "category": p.category, "price": 30.0, "price_was": None,
             "quantity_on_hand": float(MIN_STOCK - 1)},
        ])
        skus = {r["sku"] for r in self._search(category="flower")}
        self.assertNotIn("THIN-STOCK", skus)
