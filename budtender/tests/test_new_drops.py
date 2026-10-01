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


def pkg(pid, name, brand, received, *, category="Flower", batch=1, price=34, vendor="GROW OP FARMS"):
    return {"receivedDate": received, "unitPrice": price, "recUnitPrice": price,
            "batch": {"id": batch}, "vendor": {"vendorName": vendor},
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

    def test_unknown_unit_hides_potency(self):
        lab = {"Cannabinoids": {"Thc": {"Value": 80, "UnitId": 7}}}
        self.assertIsNone(new_drops.summarize_lab(lab, "Vape Cartridge")["thc"])

    def test_no_lab_data(self):
        self.assertEqual(new_drops.summarize_lab({}, "Flower"),
                         {"thc": None, "cbd": None, "potency_unit": None, "terpenes": [], "coa_url": None})


class BuildSnapshotTests(SimpleTestCase):
    def test_grouped_by_brand_newest_first_and_deduped(self):
        packages = [
            pkg(1, "Dank Czar Flower A 3.5g", "Dank Czar", "2026-09-28T10:00:00.000Z", batch=1),
            pkg(1, "Dank Czar Flower A 3.5g", "Dank Czar", "2026-09-30T10:00:00.000Z", batch=1),  # newer pkg, same product
            pkg(2, "Sungaze Beverage Lime", "Sungaze", "2026-10-01T01:00:00.000Z", category="Liquid Edible", batch=2),
            pkg(3, "Harmony Farms Trade Sample Mixed", "Harmony Farms", "2026-10-01T02:00:00.000Z", batch=3),
            pkg(4, "Free Thing", "Freebie", "2026-10-01T03:00:00.000Z", price=0, batch=4),
        ]
        snap = new_drops.build_snapshot("yakima", packages, {1: LAB_FLOWER, 2: LAB_BEVERAGE},
                                        {"1": "dank-czar-flower-a"}, NOW)
        self.assertEqual([b["brand"] for b in snap["brands"]], ["Sungaze", "Dank Czar"])  # samples/$0 dropped
        dank = snap["brands"][1]
        self.assertEqual(len(dank["products"]), 1)
        self.assertEqual(dank["products"][0]["received_at"], "2026-09-30T10:00:00.000Z")
        self.assertEqual(dank["products"][0]["menu_slug"], "dank-czar-flower-a")
        self.assertEqual(dank["vendor"], "Grow Op Farms")
        self.assertIsNone(snap["brands"][0]["products"][0]["menu_slug"])  # unmatched -> search fallback on site
        self.assertEqual(snap["generated_at"], "2026-10-01T16:00:00Z")

    def test_contract_keys_match_the_website_parser(self):
        snap = new_drops.build_snapshot("yakima", [pkg(1, "X 1g", "B", "2026-09-30T10:00:00.000Z")], {1: LAB_FLOWER}, {}, NOW)
        self.assertEqual(set(snap), {"store", "generated_at", "window_days", "brands"})
        self.assertEqual(set(snap["brands"][0]), {"brand", "vendor", "last_received", "products"})
        self.assertEqual(set(snap["brands"][0]["products"][0]),
                         {"name", "category", "strain", "strain_type", "received_at", "thc", "cbd",
                          "potency_unit", "terpenes", "price", "menu_slug", "coa_url"})


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class MenuMapTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def test_failed_refresh_keeps_last_good_map(self):
        cache.set("newdrops:menu:yakima", {"1": "kept-slug"}, None)
        with mock.patch.object(new_drops, "_fetch_menu_map", return_value=None):
            self.assertEqual(new_drops.menu_map("yakima"), {"1": "kept-slug"})

    def test_refresh_merges_and_respects_freshness_window(self):
        with mock.patch.object(new_drops, "_fetch_menu_map", return_value={"2": "new-slug"}) as f:
            self.assertEqual(new_drops.menu_map("yakima"), {"2": "new-slug"})
            new_drops.menu_map("yakima")
            self.assertEqual(f.call_count, 1)  # second call served from the 6 h window


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
