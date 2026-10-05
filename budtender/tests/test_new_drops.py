"""New Drops snapshot: potency rules, grouping/ordering, menu-slug safety, endpoint."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from budtender import new_drops
from budtender.models import Setting

NOW = datetime(2026, 10, 1, 16, 0, tzinfo=timezone.utc)

# Shapes copied from real /api/v2/batches/{id}/lab-results responses (2026-10-01).
LAB_FLOWER = {"Cannabinoids": {"Thc": {"Value": 0.4, "UnitId": 2}, "Thca": {"Value": 29.2, "UnitId": 2},
                               "Cbd": {"Value": 0.0, "UnitId": 2}, "Cbda": {"Value": 0.0, "UnitId": 2}},
              "Terpenes": {"BetaMyrcene": {"Value": 1.264, "UnitId": 2}, "BetaCaryophyllene": {"Value": 1.558, "UnitId": 2},
                           "AlphaPinene": {"Value": 0.2399, "UnitId": 2}, "Limonene": {"Value": 0.11, "UnitId": 2},
                           "Bisabolol": {"Value": None, "UnitId": None}},
              "TestDetails": {"CoaUrl": "https://certs.conflabs.com/x.pdf"}}
LAB_BEVERAGE = {"Cannabinoids": {"Thc": {"Value": 0.001, "UnitId": 2}, "Cbd": {"Value": 0.0021, "UnitId": 2}},
                "TestDetails": {"CoaUrl": "https://certs.conflabs.com/y.pdf"}}


def pkg(pid, name, brand, received, *, category="Flower", batch=1, price=34, vendor="GROW OP FARMS",
        room="Sales Floor", is_sample=False):
    return {"receivedDate": received, "unitPrice": price, "recUnitPrice": price,
            "batch": {"id": batch}, "vendor": {"vendorName": vendor},
            "room": {"roomNo": room}, "isSample": is_sample,
            "product": {"id": pid, "whseProductsDescription": name, "brand": {"brandName": brand},
                        "strain": {"strainName": "Scotch & Soda"},
                        "productTypeNavigation": {"masterCategory": category}}}


class SummarizeLabTests(SimpleTestCase):
    def test_flower_total_thc_uses_thca_conversion(self):
        out = new_drops.summarize_lab(LAB_FLOWER, "Flower")
        self.assertEqual(out["thc"], round(0.4 + 0.877 * 29.2, 1))  # 26.0, not 0.4
        self.assertIsNone(out["cbd"])  # 0% CBD is not shown
        self.assertEqual(out["potency_unit"], "%")

    def test_terpenes_top_three_sorted_and_named(self):
        out = new_drops.summarize_lab(LAB_FLOWER, "Flower")
        self.assertEqual([t["name"] for t in out["terpenes"]], ["Beta-Caryophyllene", "Beta-Myrcene", "Alpha-Pinene"])

    def test_edibles_never_show_weight_percent_potency(self):
        out = new_drops.summarize_lab(LAB_BEVERAGE, "Liquid Edible")
        self.assertIsNone(out["thc"])
        self.assertIsNone(out["potency_unit"])
        self.assertEqual(out["coa_url"], "https://certs.conflabs.com/y.pdf")  # COA still offered

    def test_a_coa_link_that_is_not_plain_https_is_dropped(self):
        for hostile in ("javascript:alert(1)", "data:text/html,<script>1</script>",
                        "http://insecure.example/x.pdf", "ftp://x.example/x.pdf"):
            lab = {**LAB_BEVERAGE, "TestDetails": {"CoaUrl": hostile}}
            self.assertIsNone(new_drops.summarize_lab(lab, "Liquid Edible")["coa_url"], hostile)

    def test_unknown_unit_hides_potency(self):
        lab = {"Cannabinoids": {"Thc": {"Value": 80, "UnitId": 7}}}
        self.assertIsNone(new_drops.summarize_lab(lab, "Vape Cartridge")["thc"])

    def test_no_lab_data(self):
        self.assertEqual(new_drops.summarize_lab({}, "Flower"),
                         {"thc": None, "cbd": None, "potency_unit": None, "terpenes": [], "coa_url": None})


class TitleTests(SimpleTestCase):
    def test_vendor_suffixes_stay_readable(self):
        self.assertEqual(new_drops._title("PAINTED ROOSTER, LLC"), "Painted Rooster, LLC")
        self.assertEqual(new_drops._title("JSM LLC"), "Jsm LLC")
        self.assertEqual(new_drops._title("CURATIONS CORPORATION"), "Curations Corporation")
        self.assertEqual(new_drops._title("Already Mixed Case"), "Already Mixed Case")


def delivered(pid, when, *, batch=None, status="Received"):
    """A receive transaction as GET /inventory/receivedinventory returns it (2026-10-04)."""
    return {"status": status, "deliveredOn": when, "vendor": "GROW OP FARMS",
            "items": [{"productId": pid, "batchId": batch, "product": f"P{pid}"}]}


def index(*txs):
    return new_drops.received_index(list(txs), NOW)


ALL = index(*(delivered(i, "2026-09-30T10:00:00.0000000", batch=i) for i in range(1, 10)))


class BuildSnapshotTests(SimpleTestCase):
    def test_grouped_by_brand_newest_first_and_deduped(self):
        packages = [
            pkg(1, "Dank Czar Flower A 3.5g", "Dank Czar", "2026-09-28T10:00:00.000Z", batch=1),
            pkg(1, "Dank Czar Flower A 3.5g", "Dank Czar", "2026-09-30T10:00:00.000Z", batch=1),  # newer pkg, same product
            pkg(2, "Sungaze Beverage Lime", "Sungaze", "2026-10-01T01:00:00.000Z", category="Liquid Edible", batch=2),
            pkg(3, "Harmony Farms Trade Sample Mixed", "Harmony Farms", "2026-10-01T02:00:00.000Z", batch=3),
            pkg(4, "Free Thing", "Freebie", "2026-10-01T03:00:00.000Z", price=0, batch=4),
        ]
        receipts = index(delivered(1, "2026-09-30T10:00:00.0000000", batch=1),
                         delivered(2, "2026-10-01T01:00:00.0000000", batch=2),
                         delivered(3, "2026-10-01T02:00:00.0000000", batch=3),
                         delivered(4, "2026-10-01T03:00:00.0000000", batch=4))
        snap = new_drops.build_snapshot("yakima", packages, {1: LAB_FLOWER, 2: LAB_BEVERAGE},
                                        {"1": "dank-czar-flower-a"}, receipts, NOW)
        self.assertEqual([b["brand"] for b in snap["brands"]], ["Sungaze", "Dank Czar"])  # samples/$0 dropped
        dank = snap["brands"][1]
        self.assertEqual(len(dank["products"]), 1)
        self.assertEqual(dank["products"][0]["received_at"], "2026-09-30T10:00:00.000Z")
        self.assertEqual(dank["products"][0]["menu_slug"], "dank-czar-flower-a")
        self.assertEqual(dank["vendor"], "Grow Op Farms")
        self.assertIsNone(snap["brands"][0]["products"][0]["menu_slug"])  # unmatched -> search fallback on site
        self.assertEqual(snap["generated_at"], "2026-10-01T16:00:00Z")

    def test_contract_keys_match_the_website_parser(self):
        snap = new_drops.build_snapshot("yakima", [pkg(1, "X 1g", "B", "2026-09-30T10:00:00.000Z")], {1: LAB_FLOWER}, {}, ALL, NOW)
        self.assertEqual(set(snap), {"store", "generated_at", "window_days", "brands"})
        self.assertEqual(set(snap["brands"][0]), {"brand", "vendor", "last_received", "products"})
        self.assertEqual(set(snap["brands"][0]["products"][0]),
                         {"name", "category", "strain", "strain_type", "received_at", "thc", "cbd",
                          "potency_unit", "terpenes", "price", "menu_slug", "coa_url"})

    def build(self, packages, receipts=ALL):
        snap = new_drops.build_snapshot("yakima", packages, {}, {}, receipts, NOW)
        return [p["name"] for b in snap["brands"] for p in b["products"]]

    # ── only real deliveries are drops (2026-10-04: returns showed as "received today") ──
    def test_a_returned_unit_does_not_make_its_product_a_new_drop(self):
        # Real data: one returned cartridge, stamped received 2026-10-04 in the returns room,
        # product last delivered long before the window.
        returned = pkg(7, "Dank Czar Cart Blue Skatalite 1g", "Dank Czar", "2026-10-01T19:38:54.000Z",
                       room="Quarantine Room/Returns")
        self.assertEqual(self.build([returned], receipts=index()), [])

    def test_a_returned_unit_never_shows_even_when_the_product_was_delivered_in_window(self):
        returned = pkg(1, "Cart 1g", "B", "2026-10-01T19:00:00.000Z", room="Quarantine Room/Returns")
        self.assertEqual(self.build([returned]), [])  # only the quarantined unit is left: nothing on the shelf

    def test_received_date_is_the_delivery_not_the_package_stamp(self):
        sellable = pkg(1, "Cart 1g", "B", "2026-10-01T19:00:00.000Z")  # package stamp = a later return
        snap = new_drops.build_snapshot("yakima", [sellable], {}, {}, index(delivered(1, "2026-09-26T09:00:00.0000000")), NOW)
        self.assertEqual(snap["brands"][0]["products"][0]["received_at"], "2026-09-26T09:00:00.000Z")

    def test_no_delivery_in_the_window_means_not_a_drop(self):
        self.assertEqual(self.build([pkg(1, "Old 1g", "B", "2026-10-01T10:00:00.000Z")],
                                    receipts=index(delivered(1, "2026-08-01T10:00:00.0000000"))), [])

    def test_a_batch_match_counts_when_the_product_id_was_remapped(self):
        self.assertEqual(self.build([pkg(99, "Remapped 1g", "B", "2026-09-30T10:00:00.000Z", batch=5)],
                                    receipts=index(delivered(1, "2026-09-30T10:00:00.0000000", batch=5))),
                         ["Remapped 1g"])

    # ── trade samples are never listed, whichever signal Dutchie happens to set ──
    def test_trade_samples_are_excluded_on_every_signal(self):
        samples = [
            pkg(1, "Fire Bros Trade Sample Mixed", "Fire Bros", "2026-09-30T10:00:00.000Z", price=0),
            pkg(2, "Fire Bros Trade Sample Mixed", "Fire Bros", "2026-09-30T10:00:00.000Z", price=25),   # name only
            pkg(3, "Fire Bros TRADE-SAMPLES Pack", "Fire Bros", "2026-09-30T10:00:00.000Z", price=25),
            pkg(4, "Fire Bros Cart 1g", "Fire Bros", "2026-09-30T10:00:00.000Z", price=25, is_sample=True),  # flag only
            pkg(5, "Fire Bros Cart 1g", "Fire Bros", "2026-09-30T10:00:00.000Z", price=0),                  # price only
        ]
        self.assertEqual(self.build(samples), [])

    def test_a_sampler_product_is_not_mistaken_for_a_trade_sample(self):
        self.assertEqual(self.build([pkg(1, "Variety Sampler 5pk", "B", "2026-09-30T10:00:00.000Z")]), ["Variety Sampler 5pk"])


class ReceivedIndexTests(SimpleTestCase):
    def test_saved_drafts_and_future_receives_are_not_deliveries(self):
        idx = index(delivered(1, "2026-10-14T17:00:00.0000000", status="Saved"),     # draft dated in the future
                    delivered(2, "2026-10-01T05:00:00.0000000", status="Saved"),     # draft, not yet received
                    delivered(3, "2026-10-05T00:00:00.0000000"),                     # "Received" but after now
                    delivered(4, "2026-09-10T00:00:00.0000000"),                     # before the 20-day window
                    delivered(5, "2026-10-01T05:00:00.0000000", batch=50))
        self.assertEqual(idx, {"p:5": "2026-10-01T05:00:00.000Z", "b:50": "2026-10-01T05:00:00.000Z"})

    def test_latest_delivery_wins_and_junk_rows_are_skipped(self):
        idx = index(delivered(1, "2026-09-25T10:00:00.0000000"), delivered(1, "2026-09-29T10:00:00.0000000"),
                    delivered(2, "not a date"), {"status": "Received", "deliveredOn": "2026-09-29T10:00:00.0000000"})
        self.assertEqual(idx, {"p:1": "2026-09-29T10:00:00.000Z"})

    def test_an_unreachable_dutchie_is_an_error_not_an_empty_week(self):
        with override_settings(DUTCHIE={"stores": {"yakima": {"pos_key": "k"}}}):
            with mock.patch.object(new_drops, "_pos_get", return_value=None):
                with self.assertRaises(RuntimeError):
                    new_drops.fetch_receipts("yakima", now=NOW)
            with mock.patch.object(new_drops, "_pos_get", return_value=[]):
                self.assertEqual(new_drops.fetch_receipts("yakima", now=NOW), [])  # authoritative "none"


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class MenuMapTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_failed_refresh_keeps_last_good_map(self):
        cache.set("newdrops:menu:yakima", {"1": "kept-slug"}, None)
        with mock.patch.object(new_drops, "_fetch_menu_map", return_value=({}, False)):
            self.assertEqual(new_drops.menu_map("yakima", now=1e9), {"1": "kept-slug"})

    def test_refresh_merges_and_respects_full_window(self):
        with mock.patch.object(new_drops, "_fetch_menu_map", return_value=({"2": "new-slug"}, True)) as f:
            self.assertEqual(new_drops.menu_map("yakima", {"2"}, now=1e9), {"2": "new-slug"})
            new_drops.menu_map("yakima", {"2"}, now=1e9 + 3 * 3600)  # nothing missing, < 6 h
            self.assertEqual(f.call_count, 1)

    def test_unmatched_arrivals_trigger_gap_refresh_but_not_too_often(self):
        with mock.patch.object(new_drops, "_fetch_menu_map", return_value=({"2": "s"}, True)) as f:
            new_drops.menu_map("yakima", {"2"}, now=1e9)
            new_drops.menu_map("yakima", {"2", "9"}, now=1e9 + 10 * 60)   # missing, but < 25 min
            self.assertEqual(f.call_count, 1)
            new_drops.menu_map("yakima", {"2", "9"}, now=1e9 + 26 * 60)   # missing and >= 25 min
            self.assertEqual(f.call_count, 2)

    def test_size_options_are_indexed_by_their_own_pos_id(self):
        page = {"data": {"filteredProducts": {"queryInfo": {"totalPages": 1}, "products": [
            {"cName": "no-mids-doh-approved-popcorn-bud-grape-z",
             "POSMetaData": {"canonicalID": "3585552", "children": [{"canonicalID": "3585552"}, {"canonicalID": "3585553"}]}}]}}}
        resp = mock.Mock(json=mock.Mock(return_value=page))
        with mock.patch("curl_cffi.requests.post", return_value=resp), mock.patch.object(new_drops.time, "sleep"):
            out, complete = new_drops._fetch_menu_map("yakima")
        self.assertTrue(complete)
        self.assertEqual(out, {"3585552": "no-mids-doh-approved-popcorn-bud-grape-z",
                               "3585553": "no-mids-doh-approved-popcorn-bud-grape-z"})


class NewDropsViewTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    @override_settings(HHT_BACKEND_TOKEN="t")
    def test_503_until_built_then_snapshot(self):
        self.client.credentials(HTTP_AUTHORIZATION="Bearer t")
        self.assertEqual(self.client.get("/api/v1/new-drops/?store=yakima").status_code, 503)
        Setting.objects.create(key="new_drops:yakima", value={"store": "yakima", "brands": []})
        r = self.client.get("/api/v1/new-drops/?store=yakima")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["store"], "yakima")

    @override_settings(HHT_BACKEND_TOKEN="t")
    def test_requires_token_and_valid_store(self):
        self.assertIn(self.client.get("/api/v1/new-drops/?store=yakima").status_code, (401, 403))
        self.client.credentials(HTTP_AUTHORIZATION="Bearer t")
        self.assertEqual(self.client.get("/api/v1/new-drops/?store=moscow").status_code, 400)


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PacingAndBudgetTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_calls_are_spaced_at_least_min_interval(self):
        sleeps = []
        client = object.__new__(new_drops.BackofficeClient)
        new_drops.BackofficeClient._last_call = 0.0
        with mock.patch.object(new_drops.PosClient, "post", return_value={"Result": True}), \
             mock.patch.object(new_drops.time, "sleep", side_effect=sleeps.append), \
             mock.patch.object(new_drops.time, "monotonic", side_effect=[100.0, 100.0, 100.1, 100.1]):
            client.post("/x", {})
            client.post("/x", {})
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], new_drops.MIN_CALL_INTERVAL - 0.1, places=5)

    def test_rate_limit_waits_and_retries_once(self):
        client = object.__new__(new_drops.BackofficeClient)
        new_drops.BackofficeClient._last_call = 0.0
        err = RuntimeError("Result=false: 'Too many requests - only 60 per minute allowed'")
        with mock.patch.object(new_drops.PosClient, "post", side_effect=[err, {"Result": True}]) as p, \
             mock.patch.object(new_drops.time, "sleep") as sl:
            self.assertEqual(client.post("/x", {}), {"Result": True})
        self.assertEqual(p.call_count, 2)
        self.assertIn(mock.call(61), sl.call_args_list)

    def test_budget_spends_lookups_on_newest_batches_first(self):
        packages = [pkg(i, f"P{i}", "B", f"2026-09-{30 - i:02d}T10:00:00.000Z", batch=100 + i) for i in range(5)]
        cache.set("newdrops:lab:104", LAB_FLOWER, 60)  # oldest batch already cached
        looked_up = []
        with mock.patch.object(new_drops, "_client"), \
             mock.patch.object(new_drops, "fetch_received", return_value=packages), \
             mock.patch.object(new_drops, "fetch_receipts", return_value=[]), \
             mock.patch.object(new_drops, "lab_for_batch", side_effect=lambda c, b: looked_up.append(b) or LAB_FLOWER), \
             mock.patch.object(new_drops, "menu_map", return_value={}), \
             mock.patch.object(new_drops.Setting.objects, "update_or_create"):
            new_drops.refresh_store("yakima", now=NOW, max_lookups=2)
        self.assertEqual(looked_up, [100, 101])  # newest two; cached 104 cost nothing


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class RunLockTests(SimpleTestCase):
    def test_overlapping_run_is_skipped(self):
        from budtender import tasks
        cache.clear()
        cache.add("newdrops:lock", 1, 60)
        self.assertEqual(tasks.refresh_new_drops_all(force=True), {"skipped": "previous_run_still_going"})
        cache.delete("newdrops:lock")


class ChatCoaTests(TestCase):
    """The chat's product cards get a COA: POS link first, else the cached lab result."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _product(self, **kw):
        from budtender.models import Product
        base = dict(sku="S1", location_slug="yakima", name="Blue Dream", price=30,
                    quantity_on_hand=5, availability=True)
        return Product.objects.create(**{**base, **kw})

    def test_public_product_coa_prefers_pos_then_lab_cache(self):
        from budtender.serializers import public_product
        cache.set("newdrops:lab:77", LAB_FLOWER)
        self.assertEqual(public_product(self._product(sku="A", batch_id="77"))["coa_url"], "https://certs.conflabs.com/x.pdf")
        self.assertEqual(public_product(self._product(sku="B", batch_id="77", coa_url="https://pos.example/c.pdf"))["coa_url"],
                         "https://pos.example/c.pdf")
        self.assertIsNone(public_product(self._product(sku="C", batch_id="78"))["coa_url"])
        cache.set("newdrops:lab:79", {"TestDetails": {"CoaUrl": "javascript:alert(1)"}})
        self.assertIsNone(public_product(self._product(sku="D", batch_id="79"))["coa_url"])

    def test_backfill_only_looks_up_uncached_in_stock_batches_without_a_pos_coa(self):
        self._product(sku="A", batch_id="1")                                   # needs a lookup
        self._product(sku="B", batch_id="1")                                   # same batch, once
        self._product(sku="C", batch_id="2", coa_url="https://pos.example/c.pdf")  # POS has it
        self._product(sku="D", batch_id="3", availability=False)               # not in stock
        self._product(sku="E", batch_id="4")
        cache.set("newdrops:lab:4", {})                                        # already looked up
        with mock.patch.object(new_drops, "_client"), \
                mock.patch.object(new_drops, "lab_for_batch") as lab:
            self.assertEqual(new_drops.backfill_lab("yakima"), 1)
        self.assertEqual([c.args[1] for c in lab.call_args_list], ["1"])

    def test_public_product_menu_slug_is_matched_on_pos_product_id(self):
        from budtender.serializers import public_product
        new_drops._SLUG_MEMO.clear()
        cache.set("newdrops:menu:yakima", {"555": "blue-dream-3-5g"})
        self.assertEqual(public_product(self._product(sku="A", product_id="555"))["menu_slug"], "blue-dream-3-5g")
        self.assertIsNone(public_product(self._product(sku="B", product_id="556"))["menu_slug"])
