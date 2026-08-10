"""Live stock/price beats the Product table on every customer-facing answer.

The failure these guard against: the beat sync refreshes `budtender_product`
every ~10 minutes, so between syncs the table happily reports a sold-out SKU as
in stock at yesterday's price â€” and the voice agent tells a caller on the phone
"yes, we have it, $25". Stock and price must come from the live sales-floor pull.

`live_stock` never hits the network under test (see `live_stock._offline`), so
each test primes the cache with the rows it wants the "live" pull to have
returned. A test that primes nothing exercises the degraded path, where the
table's own gate is correctly still in force.
"""
import json

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from budtender import live_stock
from budtender.models import Product
from budtender.serializers import PUBLIC_PRODUCT_FIELDS, public_product

TOKEN = "test-token"
LOC = "yakima"

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class _CacheIsolated(TestCase):
    """LocMemCache is process-global â€” an unnamed LOCATION means every test class
    shares one dict, so a primed store here would leak into suites that expect no
    live data. Clear on the way out as well as in."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)


def _make_product(**kw):
    defaults = dict(
        sku="A", product_id="1001", location_slug=LOC, slug="blue-dream",
        name="Blue Dream", brand="Acme", category="flower", strain_type="hybrid",
        price=25, cost=10, margin=15, quantity_on_hand=9, availability=True,
    )
    defaults.update(kw)
    return Product.objects.create(**defaults)


def _row(**kw):
    """A row shaped like `dutchie.fetch_inventory` returns."""
    defaults = dict(sku="A", product_id="1001", name="Blue Dream", brand="Acme",
                    category="flower", price=19.0, price_was=None, quantity_on_hand=7.0)
    defaults.update(kw)
    return defaults


@override_settings(CACHES=CACHES_LOCMEM)
class StockMapTests(_CacheIsolated):
    def test_no_live_data_is_not_usable_and_vetoes_nothing(self):
        m = live_stock.stock_map(LOC)
        self.assertEqual(m.source, "db")
        self.assertFalse(m.usable)
        # Critical: with no live data we must NOT veto, or a Dutchie outage
        # would empty the entire menu.
        self.assertTrue(m.buyable(sku="anything"))

    def test_primed_rows_are_indexed_by_both_join_keys(self):
        live_stock.prime(LOC, [_row()])
        m = live_stock.stock_map(LOC)
        self.assertTrue(m.usable)
        self.assertEqual(m.get(sku="A")["price"], 19.0)
        self.assertEqual(m.get(product_id="1001")["price"], 19.0)
        self.assertEqual(m.qty(sku="A"), 7.0)

    def test_unknown_sku_is_not_buyable_when_live_data_exists(self):
        live_stock.prime(LOC, [_row()])
        m = live_stock.stock_map(LOC)
        # fetch_inventory only returns sellable sales-floor rows, so absence
        # genuinely means "not on the shelf".
        self.assertFalse(m.buyable(sku="GONE"))

    def test_min_stock_is_enforced_against_live_quantity(self):
        live_stock.prime(LOC, [_row(quantity_on_hand=2.0)])
        m = live_stock.stock_map(LOC)
        self.assertTrue(m.buyable(sku="A", min_stock=2))
        self.assertFalse(m.buyable(sku="A", min_stock=5))

    def test_cost_and_margin_never_enter_a_live_row(self):
        live_stock.prime(LOC, [_row(cost=11.0, margin=8.0, unitCost=11.0)])
        m = live_stock.stock_map(LOC)
        blob = json.dumps(m.get(sku="A")).lower()
        self.assertNotIn("cost", blob)
        self.assertNotIn("margin", blob)

    def test_dutchie_unreachable_serves_the_stale_snapshot_honestly_labeled(self):
        """The degradation path when Dutchie can't be reached but we have a prior
        good pull: NOT silently "cache"/"live", NOT a fabricated number — `source`
        must say "stale" so the caller can degrade honestly (module docstring)."""
        # Seed the base cache entry directly (bypassing prime(), which also sets
        # the ":fresh" flag) so the freshness window has already lapsed, mirroring
        # what happens after TTL expiry on a real deploy.
        cache.set(live_stock._key(LOC), [_row(quantity_on_hand=7.0)], live_stock.STALE_TTL)
        m = live_stock.stock_map(LOC)
        self.assertEqual(m.source, "stale")
        self.assertTrue(m.usable)
        self.assertEqual(m.qty(sku="A"), 7.0)

    def test_dutchie_unreachable_with_no_prior_pull_ever_degrades_to_db_not_a_guess(self):
        """No cache at all (fresh install / cold store) and the pull fails (the
        `_offline()` guard makes every pull fail under test) → source="db", the
        honest "I can't confirm live" signal, not a fabricated stock number."""
        m = live_stock.stock_map(LOC)
        self.assertEqual(m.source, "db")
        self.assertFalse(m.usable)


@override_settings(CACHES=CACHES_LOCMEM)
class PublicProductOverlayTests(_CacheIsolated):
    def test_live_price_and_stock_win_over_the_table(self):
        p = _make_product(price=25, quantity_on_hand=9)
        out = public_product(p, live=_row(price=19.0, quantity_on_hand=7.0))
        self.assertEqual(out["price"], 19.0)
        self.assertEqual(out["stock_on_hand"], 7)

    def test_overlay_does_not_widen_the_allowlist(self):
        p = _make_product()
        out = public_product(p, live=_row(cost=11.0))
        self.assertEqual(set(out.keys()), set(PUBLIC_PRODUCT_FIELDS))

    def test_without_live_the_table_values_are_used(self):
        p = _make_product(price=25, quantity_on_hand=9)
        out = public_product(p)
        self.assertEqual(out["price"], 25)
        self.assertEqual(out["stock_on_hand"], 9)


@override_settings(HHT_BACKEND_TOKEN=TOKEN, CACHES=CACHES_LOCMEM)
class VoiceInventoryEndpointTests(_CacheIsolated):
    """`/products/by-sku/` IS the voice agent's check_inventory."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        _make_product()

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

    def _by_sku(self, sku="A"):
        return self.client.get("/api/v1/products/by-sku/",
                               {"store": LOC, "sku": sku}, **self._auth())

    def test_sold_out_live_is_not_offered_even_though_the_table_says_in_stock(self):
        # The exact production failure: table says 9 on hand, floor says 0.
        live_stock.prime(LOC, [_row(sku="OTHER", product_id="2002")])
        r = self._by_sku()
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("product", r.json())
        self.assertEqual(r.json()["stock_source"], "cache")

    def test_live_price_is_quoted_not_the_table_price(self):
        live_stock.prime(LOC, [_row(price=19.0, quantity_on_hand=7.0)])
        r = self._by_sku()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["price"], 19.0)
        self.assertEqual(r.json()["product"]["stock_on_hand"], 7)

    def test_degrades_to_the_table_when_there_is_no_live_data(self):
        r = self._by_sku()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["sku"], "A")
        self.assertEqual(r.json()["stock_source"], "db")

    def test_response_never_leaks_cost_or_margin(self):
        live_stock.prime(LOC, [_row()])
        blob = self._by_sku().content.decode().lower()
        self.assertNotIn("margin", blob)
        self.assertNotIn("cost", blob)


@override_settings(HHT_BACKEND_TOKEN=TOKEN, CACHES=CACHES_LOCMEM)
class InStockSlugsEndpointTests(_CacheIsolated):
    def setUp(self):
        super().setUp()
        self.client = Client()
        _make_product()

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

    def test_slugs_come_from_live_stock_when_available(self):
        live_stock.prime(LOC, [_row(name="OG Kush", sku="B", product_id="2002",
                                    quantity_on_hand=6.0)])
        body = self.client.get("/api/v1/products/in-stock/", {"store": LOC},
                               **self._auth()).json()
        # "Blue Dream" is in the table but not on the floor, so it must not be
        # advertised as in stock.
        self.assertEqual(body["slugs"], ["og-kush"])
        self.assertEqual(body["stock_source"], "cache")

    def test_live_rows_below_min_stock_are_excluded(self):
        live_stock.prime(LOC, [_row(quantity_on_hand=1.0)])
        body = self.client.get("/api/v1/products/in-stock/", {"store": LOC},
                               **self._auth()).json()
        self.assertEqual(body["slugs"], [])

    def test_degrades_to_the_table_when_there_is_no_live_data(self):
        body = self.client.get("/api/v1/products/in-stock/", {"store": LOC},
                               **self._auth()).json()
        self.assertEqual(body["slugs"], ["blue-dream"])
        self.assertEqual(body["stock_source"], "db")


@override_settings(HHT_BACKEND_TOKEN=TOKEN, CACHES=CACHES_LOCMEM)
class PhoneCartQuoteTests(_CacheIsolated):
    def setUp(self):
        super().setUp()
        self.client = Client()
        _make_product()

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

    def _add(self, sku="A"):
        return self.client.post(
            "/api/v1/phone-cart/upsert",
            data=json.dumps({"call_id": "c1", "location": LOC, "action": "add_item",
                             "sku": sku, "quantity": 1}),
            content_type="application/json", **self._auth())

    def test_sold_out_live_is_refused_even_though_the_table_says_in_stock(self):
        live_stock.prime(LOC, [_row(sku="OTHER", product_id="2002")])
        r = self._add()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "not_in_stock")

    def test_quote_uses_the_live_price_and_labels_its_source(self):
        live_stock.prime(LOC, [_row(price=19.0)])
        r = self._add()
        self.assertEqual(r.status_code, 200)
        draft = r.json()["draft"]
        self.assertEqual(draft["lines"][0]["unit_price"], 19.0)
        self.assertEqual(draft["lines"][0]["quote_source"], "live_sales_floor")
        self.assertEqual(draft["quote"]["source"], "live_sales_floor")

    def test_quote_source_is_honest_when_it_fell_back_to_the_table(self):
        r = self._add()
        self.assertEqual(r.status_code, 200)
        draft = r.json()["draft"]
        self.assertEqual(draft["lines"][0]["unit_price"], 25.0)
        # The old code hardcoded "current_public_product_price" here regardless
        # of source, which made a stale quote unfalsifiable.
        self.assertEqual(draft["quote"]["source"], "budtender_product")

