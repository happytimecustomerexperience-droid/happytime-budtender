"""
`budtender/dutchie.py` has no dedicated test file anywhere in this suite — every
other test (`test_live_stock.py`, `test_customer_history_sync.py`, ...) primes
`live_stock`/mocks `dutchie.fetch_inventory` itself directly, so `dutchie.py`'s OWN
parsing/normalization (`fetch_inventory`, `_norm_category`, `_is_purchasable`,
`_sales_floor_qty`, `_off_menu_product_ids`, `_stale`) never actually runs under
test. These tests mock ONLY the HTTP boundary (`requests.get`) so that logic runs
for real, against payload shapes matching what `dutchie.py`'s own field lookups
expect (there is no captured real POS payload for `/reporting/inventory` or
`/products` in the repo to reuse — the one committed fixture set,
`dutchie/fixtures/*.json`, is a different API surface: the in-store register's
PascalCase `product_SearchV2`/cart/lab endpoints, not this REST client's
camelCase `/reporting/inventory` + `/products`).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from budtender import dutchie

LOC = "yakima"
FRESH_DATE = datetime.now(timezone.utc).date().isoformat()
STALE_DATE = (datetime.now(timezone.utc).date() - timedelta(days=90)).isoformat()

_DUTCHIE_SETTINGS = {
    "stores": {LOC: {"pos_key": "test-key", "loc_id": "1", "lsp_id": "1"}},
}


def _inv_row(**kw):
    """One /reporting/inventory row shaped per dutchie.py's own field lookups."""
    row = {
        "productId": 111,
        "sku": "SKU-111",
        "productName": "Blueberry OG 3.5g",
        "brandName": "Phat Panda",
        "category": "DOH Approved Flower",
        "strainName": "Blueberry OG",
        "strainType": "indica",
        "unitPrice": 38.0,
        "recUnitPrice": 38.0,
        "unitCost": 14.0,
        "unitWeight": 3.5,
        "lastModifiedDateUtc": FRESH_DATE,
        "medicalOnly": False,
        "roomQuantities": [
            {"room": "Sales Floor", "quantityAvailable": 12},
            {"room": "Quarantine Room/Returns", "quantityAvailable": 500},
        ],
        "labResults": [{"name": "THC", "value": 27.3}],
    }
    row.update(kw)
    return row


def _product_row(**kw):
    """One /products catalog row (drives _off_menu_product_ids)."""
    row = {
        "productId": 111,
        "isActive": True,
        "onlineAvailable": True,
        "onlineProduct": True,
        "ecomCategory": "Flower",
        "ecomSubcategory": "Indica",
    }
    row.update(kw)
    return row


def _get(inventory_rows, product_rows):
    """A requests.get stand-in routed by path, mirroring dutchie._pos_get's contract."""

    def _fake_get(url, params=None, headers=None, auth=None, timeout=None):
        resp = mock.Mock()
        resp.raise_for_status = mock.Mock()
        if url.endswith("/reporting/inventory"):
            resp.json.return_value = inventory_rows
        elif url.endswith("/inventory"):
            resp.json.return_value = []  # empty -> caller falls back to /reporting/inventory
        elif url.endswith("/products"):
            resp.json.return_value = product_rows
        else:
            raise AssertionError(f"unexpected path: {url}")
        return resp

    return _fake_get


@override_settings(DUTCHIE=_DUTCHIE_SETTINGS)
class FetchInventoryParsingTests(TestCase):
    def setUp(self):
        super().setUp()
        # _off_menu_product_ids caches per-location for an hour (LocMemCache is
        # process-global) — without clearing, a later test's /products payload is
        # never actually fetched, it just replays the first test's cached set.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_normal_row_round_trips_with_sales_floor_qty_and_mapped_category(self):
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get([_inv_row()], [_product_row()])):
            rows = dutchie.fetch_inventory(LOC)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # Sales Floor only (12), NOT the 500 sitting in Quarantine/Returns.
        self.assertEqual(r["quantity_on_hand"], 12)
        self.assertEqual(r["category"], "flower")
        self.assertEqual(r["price"], 38.0)
        self.assertEqual(r["thc_percent"], 27.3)
        self.assertEqual(r["strain"], "Blueberry OG")

    def test_category_mapping_covers_the_documented_examples(self):
        cases = [
            ("Disposable Vape", "vape-cartridges"),
            ("Infused Pre-Roll", "pre-rolls"),
            ("RSO", "concentrates"),
            ("DOH Approved Flower", "flower"),
            ("Wyld Gummies", "edibles"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                rows = [_inv_row(productId=200, sku="X", category=raw)]
                with mock.patch("budtender.dutchie.requests.get",
                                side_effect=_get(rows, [_product_row(productId=200)])):
                    out = dutchie.fetch_inventory(LOC)
                self.assertEqual(out[0]["category"], expected)

    def test_zero_sales_floor_stock_never_comes_back(self):
        rows = [_inv_row(roomQuantities=[{"room": "Sales Floor", "quantityAvailable": 0},
                                         {"room": "Back Stock", "quantityAvailable": 40}])]
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get(rows, [_product_row()])):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_quarantine_and_back_stock_never_count_toward_purchasable_qty(self):
        # Sales Floor present but 0; everything else in other rooms. Must be dropped,
        # not fall back to the (nonzero) all-rooms aggregate, since a room breakdown IS present.
        rows = [_inv_row(roomQuantities=[{"room": "Sales Floor", "quantityAvailable": 0},
                                         {"roomName": "Vault", "quantity": 999}])]
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get(rows, [_product_row()])):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_medical_only_is_dropped(self):
        rows = [_inv_row(medicalOnly=True)]
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get(rows, [_product_row()])):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_priceless_row_is_dropped(self):
        rows = [_inv_row(unitPrice=0, recUnitPrice=0)]
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get(rows, [_product_row()])):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_stale_zombie_stock_is_dropped(self):
        rows = [_inv_row(lastModifiedDateUtc=STALE_DATE)]
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get(rows, [_product_row()])):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_unparseable_last_modified_date_fails_open_and_is_kept(self):
        rows = [_inv_row(lastModifiedDateUtc="not-a-date")]
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get(rows, [_product_row()])):
            out = dutchie.fetch_inventory(LOC)
        self.assertEqual(len(out), 1)

    # ── off-menu filter (/products) ──────────────────────────────────────────
    def test_retired_product_is_excluded(self):
        rows = [_inv_row()]
        prods = [_product_row(isActive=False)]
        with mock.patch("budtender.dutchie.requests.get", side_effect=_get(rows, prods)):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_not_online_available_is_excluded(self):
        rows = [_inv_row()]
        prods = [_product_row(onlineAvailable=False)]
        with mock.patch("budtender.dutchie.requests.get", side_effect=_get(rows, prods)):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_no_ecom_category_at_all_is_excluded(self):
        rows = [_inv_row()]
        prods = [_product_row(ecomCategory=None, ecomSubcategory="N/A")]
        with mock.patch("budtender.dutchie.requests.get", side_effect=_get(rows, prods)):
            self.assertEqual(dutchie.fetch_inventory(LOC), [])

    def test_ecom_subcategory_alone_is_enough_to_keep_it(self):
        rows = [_inv_row()]
        prods = [_product_row(ecomCategory=None, ecomSubcategory="Indica")]
        with mock.patch("budtender.dutchie.requests.get", side_effect=_get(rows, prods)):
            out = dutchie.fetch_inventory(LOC)
        self.assertEqual(len(out), 1)

    def test_products_fetch_failure_fails_open_not_closed(self):
        """A broken /products call must not empty the whole catalog — fail open."""

        def _fake_get(url, params=None, headers=None, auth=None, timeout=None):
            resp = mock.Mock()
            resp.raise_for_status = mock.Mock()
            if url.endswith("/reporting/inventory"):
                resp.json.return_value = [_inv_row()]
            elif url.endswith("/inventory"):
                resp.json.return_value = []
            elif url.endswith("/products"):
                resp.json.return_value = {"error": "not a list"}
            return resp

        with mock.patch("budtender.dutchie.requests.get", side_effect=_fake_get):
            out = dutchie.fetch_inventory(LOC)
        self.assertEqual(len(out), 1)

    # ── aggregation ───────────────────────────────────────────────────────────
    def test_same_product_id_across_packages_aggregates_quantity(self):
        rows = [
            _inv_row(sku="PKG-A"),
            _inv_row(sku="PKG-B", roomQuantities=[{"room": "Sales Floor", "quantityAvailable": 5}]),
        ]
        with mock.patch("budtender.dutchie.requests.get",
                        side_effect=_get(rows, [_product_row()])):
            out = dutchie.fetch_inventory(LOC)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["quantity_on_hand"], 17)  # 12 + 5

    def test_no_pos_key_returns_empty_without_calling_the_network(self):
        with override_settings(DUTCHIE={"stores": {LOC: {}}}):
            with mock.patch("budtender.dutchie.requests.get") as m:
                self.assertEqual(dutchie.fetch_inventory(LOC), [])
            m.assert_not_called()
