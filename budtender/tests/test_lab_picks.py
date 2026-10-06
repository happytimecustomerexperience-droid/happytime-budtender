"""Picks carry real lab numbers (Contract A) and 'stronger'/'cheaper' re-rank in place (Contract B).

Serializer shape, the single bulk lab read per request, `sort_by` through the real HTTP path, and
the ranker rules (potency desc nulls last / price asc; every other filter keeps applying).
Labs are stored rows, built from the real-shaped payloads in test_new_drops.py. No network.
"""
import json
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from budtender import lab_enrich, views
from budtender.models import BatchLab, Product
from budtender.ranking import rank_products
from budtender.serializers import PUBLIC_PRODUCT_FIELDS, public_product
from budtender.tests.test_new_drops import LAB_FLOWER

TOKEN = "test-token"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
NOW = timezone.now()


def _flower(sku, *, thc=None, price=30, batch="", category="flower", **kw):
    defaults = dict(location_slug="yakima", name=f"Flower {sku}", brand=f"Brand {sku}", category=category,
                    price=price, cost=10, margin=20, quantity_on_hand=10, availability=True,
                    thc_percent=thc, batch_id=batch)
    defaults.update(kw)
    return Product.objects.create(sku=sku, **defaults)


def _lab(batch, *, thc_total=None, terpenes=(("Beta-Myrcene", 0.82),)):
    data = {"total_terpenes": round(sum(p for _, p in terpenes), 2) or None,
            "terpenes": [{"name": n, "pct": p} for n, p in terpenes],
            "cbd_total": None, "thc_total": thc_total, "tested_date": "2026-09-14",
            "lab_name": "Test Lab", "coa_url": "https://certs.example.com/a.pdf"}
    BatchLab.objects.create(batch_id=batch, status="ok", data=data, checked_at=NOW)
    return data


class SerializerLabTests(TestCase):
    def test_lab_is_part_of_the_public_allowlist(self):
        self.assertIn("lab", PUBLIC_PRODUCT_FIELDS)

    def test_a_real_lab_emits_contract_a_with_a_profile_built_from_the_numbers(self):
        p = _flower("A", thc=24.0, batch="900")
        stored = lab_enrich.lab_from_data(LAB_FLOWER, "flower")
        out = public_product(p, lab=stored)
        self.assertEqual(out["lab"], {
            "total_terpenes": 3.17,
            "terpenes": [{"name": "Beta-Caryophyllene", "pct": 1.56}, {"name": "Beta-Myrcene", "pct": 1.26},
                         {"name": "Alpha-Pinene", "pct": 0.24}, {"name": "Limonene", "pct": 0.11}],
            "cbd_total": None, "minor_cannabinoids": [], "tested_date": None,
            "lab_name": None, "coa_url": "https://certs.conflabs.com/x.pdf", "contaminants": {},
            "profile": {"lean": None, "notes": ["pepper", "earthy", "pine"],
                        "line": "Caryophyllene-led (1.56%) — pepper.",
                        "explain": "Smells spicy, earthy and piney (caryophyllene, myrcene, pinene).",
                        "aroma": ["spicy", "earthy", "pine"]}})
        self.assertNotIn("profile", stored)  # the stored row is never mutated
        self.assertEqual(stored["thc_total"], 26.0)  # ...and it still keeps thc_total (storage only)
        print("\nLAB SHAPE EMITTED:", json.dumps(out["lab"], ensure_ascii=False))

    def test_the_serialized_lab_has_one_potency_number_thc_percent_not_a_second_thc_total(self):
        stored = lab_enrich.lab_from_data(LAB_FLOWER, "flower")
        out = public_product(_flower("A", thc=None, batch="900"), lab=stored)
        self.assertNotIn("thc_total", out["lab"])
        self.assertEqual(out["thc_percent"], 26.0)                       # the lab value still fills the one number
        out = public_product(_flower("B", thc=24.0, batch="901"), lab=stored)
        self.assertNotIn("thc_total", out["lab"])
        self.assertEqual(out["thc_percent"], 24.0)                       # inventory still wins

    def test_an_extra_key_in_a_stored_lab_never_reaches_a_customer(self):
        stored = {**lab_enrich.lab_from_data(LAB_FLOWER, "flower"),
                  "internal_note": "SECRET", "cost": 99, "vendor": "FIXTURE VENDOR",
                  "terpenes": [{"name": "Beta-Myrcene", "pct": 1.2, "secret": "x"}, {"name": 7, "pct": 1},
                              {"name": "Bad", "pct": "high"}, "junk"],
                  "minor_cannabinoids": [{"name": "CBG", "pct": 1.5, "extra": 1}, {"name": "X", "pct": -1}],
                  "contaminants": {"pesticides": "pass", "evil": "pass", "mycotoxin": "fail"},
                  "coa_url": "javascript:alert(1)", "tested_date": "tomorrow-ish", "lab_name": 5}
        lab = public_product(_flower("A", batch="900"), lab=stored)["lab"]
        self.assertEqual(set(lab), {"total_terpenes", "terpenes", "cbd_total", "minor_cannabinoids", "tested_date",
                                    "lab_name", "coa_url", "contaminants", "profile"})
        self.assertEqual(lab["terpenes"], [{"name": "Beta-Myrcene", "pct": 1.2}])
        self.assertEqual(lab["minor_cannabinoids"], [{"name": "CBG", "pct": 1.5}])
        self.assertEqual(lab["contaminants"], {"pesticides": "pass"})
        self.assertEqual((lab["coa_url"], lab["tested_date"], lab["lab_name"]), (None, None, None))
        blob = json.dumps(lab)
        for word in ("SECRET", "FIXTURE VENDOR", "internal", "secret", "vendor", "javascript"):
            self.assertNotIn(word, blob)

    def test_size_is_the_exact_gram_weight_never_a_bucket(self):
        # ranking.size_label snaps to a bucket (0.8g -> "1g", 3.4g -> "3.5g"): fine for filtering, a misstatement
        # on a card. The card gets the exact figure.
        for weight, label in ((3.5, "3.5g"), (28.0, "28g"), (0.8, "0.8g"), (1.0, "1g"), (7.0, "7g"), (0.5, "0.5g"),
                              (3.4, "3.4g"), (14.0, "14g")):
            self.assertEqual(public_product(_flower(f"W{weight}", unit_weight=weight))["size"], label, weight)
        self.assertEqual(public_product(_flower("B", unit_weight=1.0, category="vape-cartridges"))["size"], "1g")
        self.assertEqual(public_product(_flower("PR", unit_weight=0.5, category="pre-rolls"))["size"], "0.5g")
        self.assertEqual(public_product(_flower("CO", unit_weight=1.0, category="concentrates"))["size"], "1g")
        self.assertIn("size", PUBLIC_PRODUCT_FIELDS)

    def test_a_real_weight_in_the_name_wins_over_a_mislabeled_unit_weight_like_the_size_filter(self):
        p = _flower("M", unit_weight=3.5, name="White Cherries 14g")
        self.assertEqual(public_product(p)["size"], "14g")

    def test_size_is_null_when_there_is_no_trustworthy_figure(self):
        self.assertIsNone(public_product(_flower("D"))["size"])                         # no weight on file
        self.assertIsNone(public_product(_flower("Z", unit_weight=0))["size"])
        # edibles/beverages/tinctures: potency_mg is package-total and noisy, so no mg bucket is ever invented
        for category in ("edibles", "beverages", "tinctures", "topicals", "capsules"):
            p = _flower(f"E-{category}", category=category, potency_mg=10.0, unit_weight=28.0)
            self.assertIsNone(public_product(p)["size"], category)
        self.assertIsNone(public_product(_flower("U", category="mints", unit_weight=1.0))["size"])

    def test_info_is_null_when_nothing_is_stored_and_the_stored_allowlist_otherwise(self):
        self.assertIn("info", PUBLIC_PRODUCT_FIELDS)
        p = _flower("A")
        self.assertIsNone(public_product(p)["info"])
        self.assertIsNone(public_product(p, info={})["info"])
        info = {"strain_type": "Hybrid", "tags": ["1g"]}
        self.assertEqual(public_product(p, info=info)["info"], info)

    def test_no_lab_on_file_is_null_never_a_zero_filled_dict(self):
        out = public_product(_flower("A", thc=24.0))
        self.assertIsNone(out["lab"])
        self.assertEqual(set(out), set(PUBLIC_PRODUCT_FIELDS))

    def test_every_key_is_on_the_allowlist_when_a_lab_is_present(self):
        out = public_product(_flower("A", batch="900"), lab=lab_enrich.lab_from_data(LAB_FLOWER, "flower"))
        self.assertEqual(set(out), set(PUBLIC_PRODUCT_FIELDS))

    def test_thc_percent_is_the_inventory_number_when_it_has_one(self):
        out = public_product(_flower("A", thc=20.0), lab={"thc_total": 26.0, "terpenes": []})
        self.assertEqual(out["thc_percent"], 20.0)

    def test_thc_percent_falls_back_to_the_lab_only_when_inventory_has_none(self):
        out = public_product(_flower("A", thc=None), lab={"thc_total": 26.0, "terpenes": []})
        self.assertEqual(out["thc_percent"], 26.0)
        self.assertIsNone(public_product(_flower("B", thc=None), lab={"thc_total": None, "terpenes": []})["thc_percent"])
        self.assertIsNone(public_product(_flower("C", thc=None))["thc_percent"])

    def test_a_lab_with_no_terpenes_still_has_a_well_formed_profile(self):
        out = public_product(_flower("A"), lab={"thc_total": 22.0, "terpenes": [], "coa_url": "https://x.example/a.pdf"})
        self.assertEqual(out["lab"]["profile"], {"lean": None, "notes": [], "line": "", "explain": "", "aroma": []})

    def test_cost_and_margin_never_appear_beside_a_lab(self):
        p = _flower("A", batch="900", cost=12, margin=18)
        blob = json.dumps(public_product(p, lab=lab_enrich.lab_from_data(LAB_FLOWER, "flower"))).lower()
        for word in ("margin", "cost"):
            self.assertNotIn(word, blob)


class RankerSortTests(TestCase):
    """Contract B. A: 18% $30 · B: 28% $55 · C: no THC $20 · D: 24% $40 · E: no THC but a 31% lab $45 ·
    F: 35% (excluded) · G: 40% but $90 (outside the budget band) · H: a pre-roll at 99%."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _flower("A", thc=18.0, price=30)
        _flower("B", thc=28.0, price=55)
        _flower("C", thc=None, price=20)
        _flower("D", thc=24.0, price=40)
        _flower("E", thc=None, price=45, batch="E1")
        _lab("E1", thc_total=31.0)
        _flower("F", thc=35.0, price=35)
        _flower("G", thc=40.0, price=90)
        _flower("H", thc=99.0, price=30, category="pre-rolls")

    def _skus(self, **kw):
        slots = {"category": "flower", "price_max": 60, **kw.pop("slots", {})}
        ranked = rank_products("yakima", slots, None, limit=kw.pop("limit", 10),
                               exclude_skus=kw.pop("exclude", {"F"}))
        return [p.sku for p, _why in ranked]

    def test_potency_is_highest_first_with_unknowns_last_and_a_lab_filling_a_missing_thc(self):
        # E sorts as 31 (its lab), C has nothing anywhere -> last.
        self.assertEqual(self._skus(slots={"sort_by": "potency"}), ["E", "B", "D", "A", "C"])

    def test_price_asc_is_cheapest_first(self):
        self.assertEqual(self._skus(slots={"sort_by": "price_asc"}), ["C", "A", "D", "E", "B"])

    def test_every_other_filter_keeps_applying(self):
        got = self._skus(slots={"sort_by": "potency"})
        self.assertNotIn("F", got)   # exclude_skus
        self.assertNotIn("G", got)   # 40% but over the price band
        self.assertNotIn("H", got)   # a different category at 99%
        self.assertNotIn("G", self._skus(slots={"sort_by": "price_asc"}))

    def test_the_price_tier_band_still_applies_under_a_sort(self):
        got = rank_products("yakima", {"category": "flower", "price_tier": "mid", "sort_by": "potency"}, None,
                            limit=10)
        self.assertEqual([p.sku for p, _ in got], ["F", "D", "A", "C"])  # the $20-40 band only, strongest first
        self.assertTrue(all(20 <= float(p.price) <= 40 for p, _ in got))

    def test_limit_takes_the_top_of_the_sorted_set(self):
        self.assertEqual(self._skus(slots={"sort_by": "potency"}, limit=2), ["E", "B"])

    def test_an_unknown_sort_is_ignored_not_an_error_and_not_a_reorder(self):
        base = self._skus()
        for junk in ("loudest", "", None, 7, ["potency"], {"x": 1}):
            self.assertEqual(self._skus(slots={"sort_by": junk}), base, junk)

    def test_sort_by_wins_over_premium_price_ordering_but_the_budget_floor_still_holds(self):
        # limit=5 on purpose: with limit=3 the $30 and $20 picks (cheaper than the "top" floor of $40) were
        # pushed out of the window and a missing price filter looked fine.
        got = rank_products("yakima", {"category": "flower", "price_tier": "top", "sort_by": "potency"}, None,
                            limit=5, exclude_skus={"F"})
        self.assertEqual([p.sku for p, _ in got], ["G", "E", "B", "D"])   # 40, 31, 28, 24: strongest first,
        self.assertTrue(all(float(p.price) >= 40 for p, _ in got))        # and nothing under the top-tier floor

    def test_a_dollar_floor_of_100_or_more_is_a_hard_filter_under_sort_by_too(self):
        _flower("X", thc=10.0, price=120)
        for mode in ("potency", "price_asc"):
            got = rank_products("yakima", {"category": "flower", "price_min": 100, "sort_by": mode}, None,
                                limit=5, exclude_skus={"F"})
            self.assertEqual([p.sku for p, _ in got], ["X"], mode)

    def test_without_sort_by_the_premium_preference_is_unchanged(self):
        # premium intent keeps its existing meaning: price is a PREFERENCE (priciest first), not a gate
        got = rank_products("yakima", {"category": "flower", "price_tier": "top"}, None, limit=5, exclude_skus={"F"})
        self.assertEqual([p.sku for p, _ in got][:2], ["G", "B"])
        self.assertIn(len(got), (5,))

    def test_a_tie_is_broken_deterministically(self):
        _flower("T1", thc=24.0, price=40)
        _flower("T2", thc=24.0, price=40)
        first = self._skus(slots={"sort_by": "potency"})
        self.assertEqual(first, self._skus(slots={"sort_by": "potency"}))

    def test_the_reason_is_the_engines_even_when_a_lab_exists_the_lab_words_live_in_the_profile(self):
        got = dict((p.sku, why) for p, why in rank_products(
            "yakima", {"category": "flower", "price_max": 60, "sort_by": "potency"}, None, limit=10,
            exclude_skus={"F"}))
        self.assertTrue(got["E"])
        for sku, why in got.items():
            self.assertNotIn("-led", why, sku)                  # never the lab line
            self.assertNotIn(f"Flower {sku}", why, sku)         # never the product name


@override_settings(CACHES=CACHES_LOCMEM, HHT_BACKEND_TOKEN=TOKEN)
class SearchViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        patcher = mock.patch.object(views, "inventory_is_stale", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _search(self, slots=None, **extra):
        resp = self.client.post(
            "/api/v1/products/search/",
            data=json.dumps({"slots": {"store": "yakima", "category": "flower", **(slots or {})}, "limit": 5, **extra}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["results"]

    def test_a_pick_with_a_lab_carries_it_and_one_without_is_null(self):
        _flower("WITH", thc=24.0, price=30, batch="B1")
        _lab("B1", thc_total=24.2)
        _flower("WITHOUT", thc=22.0, price=31)
        by_sku = {r["sku"]: r for r in self._search()}
        self.assertEqual(by_sku["WITH"]["lab"]["terpenes"], [{"name": "Beta-Myrcene", "pct": 0.82}])
        self.assertEqual(by_sku["WITH"]["lab"]["profile"]["lean"], "relaxing")
        self.assertEqual(by_sku["WITH"]["thc_percent"], 24.0)       # inventory wins
        self.assertNotIn("-led", by_sku["WITH"]["why_this"])      # the lab words are in lab.profile, once
        self.assertNotIn("Flower WITH", by_sku["WITH"]["why_this"])
        self.assertIsNone(by_sku["WITHOUT"]["lab"])

    def test_an_extra_key_in_a_stored_row_never_reaches_the_endpoint_response(self):
        _flower("A", thc=24.0, batch="B1")
        row = _lab("B1", thc_total=24.2)
        BatchLab.objects.filter(batch_id="B1").update(data={**row, "internal_note": "SECRET", "cost": 9})
        body = json.dumps(self._search())
        for word in ("SECRET", "internal_note", '"cost"', "thc_total"):
            self.assertNotIn(word, body)

    def test_the_lab_fills_a_missing_thc_through_the_endpoint(self):
        _flower("NOTHC", thc=None, price=30, batch="B2")
        _lab("B2", thc_total=27.5)
        self.assertEqual(self._search()[0]["thc_percent"], 27.5)

    def test_labs_are_read_in_one_bulk_query_not_one_per_product(self):
        for i in range(5):
            _flower(f"P{i}", thc=20.0 + i, price=30 + i, batch=f"B{i}")
            _lab(f"B{i}", thc_total=20.0 + i)
        with CaptureQueriesContext(connection) as ctx:
            results = self._search()
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r["lab"] for r in results))
        lab_queries = [q["sql"] for q in ctx.captured_queries if "budtender_batchlab" in q["sql"]]
        self.assertEqual(len(lab_queries), 1, lab_queries)

    def test_a_potency_sort_costs_at_most_one_extra_bulk_query(self):
        for i in range(5):
            _flower(f"P{i}", thc=None if i == 0 else 20.0 + i, price=30 + i, batch=f"B{i}")
            _lab(f"B{i}", thc_total=20.0 + i)
        with CaptureQueriesContext(connection) as ctx:
            results = self._search({"sort_by": "potency"})
        self.assertEqual([r["sku"] for r in results], ["P4", "P3", "P2", "P1", "P0"])
        lab_queries = [q["sql"] for q in ctx.captured_queries if "budtender_batchlab" in q["sql"]]
        self.assertLessEqual(len(lab_queries), 2, lab_queries)

    def test_sort_by_reaches_the_ranker_through_the_view(self):
        for sku, thc, price in (("LOW", 15.0, 20), ("HIGH", 30.0, 45), ("MID", 22.0, 30)):
            _flower(sku, thc=thc, price=price)
        self.assertEqual([r["sku"] for r in self._search({"sort_by": "potency"})], ["HIGH", "MID", "LOW"])
        self.assertEqual([r["sku"] for r in self._search({"sort_by": "price_asc"})], ["LOW", "MID", "HIGH"])

    def test_stronger_within_budget_excluding_what_was_already_shown(self):
        for sku, thc, price in (("SHOWN", 30.0, 40), ("NEXT", 27.0, 42), ("OVER", 33.0, 90), ("WEAK", 12.0, 30)):
            _flower(sku, thc=thc, price=price)
        got = self._search({"sort_by": "potency", "price_max": 50}, exclude_skus=["SHOWN"])
        self.assertEqual([r["sku"] for r in got], ["NEXT", "WEAK"])

    def test_no_cost_or_margin_in_a_search_that_carries_labs(self):
        _flower("A", thc=24.0, batch="B1", cost=12, margin=18)
        _lab("B1", thc_total=24.0)
        blob = json.dumps(self._search()).lower()
        for word in ("margin", "cost"):
            self.assertNotIn(word, blob)

    def test_by_sku_carries_the_lab_too(self):
        _flower("A", thc=24.0, price=30, batch="B1", slug="a")
        _lab("B1", thc_total=24.0)
        r = self.client.get("/api/v1/products/by-sku/", {"store": "yakima", "sku": "A"},
                            HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["product"]["lab"]["terpenes"], [{"name": "Beta-Myrcene", "pct": 0.82}])
        self.assertEqual(r.json()["product"]["lab"]["profile"]["lean"], "relaxing")

    def test_pairing_carries_the_lab_too(self):
        anchor = _flower("ANCHOR", price=30, slug="anchor")
        pair = _flower("PAIR", thc=None, price=12, batch="B9", slug="pair", category="edibles")
        _lab("B9", thc_total=None, terpenes=(("Beta-Myrcene", 0.4),))
        with mock.patch.object(views, "pair_for", return_value=(pair, "complement", "goes well", 0.5)):
            r = self.client.post("/api/v1/pairing/for-sku",
                                 data=json.dumps({"location": "yakima", "sku": anchor.sku}),
                                 content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json()["pairing"]["lab"]["profile"]["lean"], "relaxing")
