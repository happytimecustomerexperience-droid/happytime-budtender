"""Search v2 (docs/contracts/search-v2.md): master categories, the v2 slots, viability-aware options,
offset paging capped at 20, and find-similar with full cards.

The two invariants every test here leans on:
  * nothing that is not in stock (availability AND >= MIN_STOCK, live pull when usable) is ever returned,
    counted or offered;
  * an option is listed only when following it leads to >= 1 product, and its count is what the search finds.
"""
import json

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from budtender import facets, live_stock, product_attrs, ranking
from budtender.models import BatchLab, ChatSession, CustomerProfile, Product, ProductDetail, SyncState
from budtender.ranking import MIN_STOCK, SEARCH_CAP

TOKEN = "test-token"
LOC = "yakima"
AUTH = {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}
_n = {"i": 0}


def _p(name, category, *, price=30, qty=10, available=True, lab=None, info=None, **kw):
    _n["i"] += 1
    i = _n["i"]
    p = Product.objects.create(
        sku=kw.pop("sku", f"S{i}"), product_id=f"{9000 + i}", batch_id=f"{7000 + i}", location_slug=LOC,
        name=name, category=category, price=price, cost=price / 3, margin=price - price / 3,
        quantity_on_hand=qty, availability=available, slug=kw.pop("slug", f"s{i}"), **kw)
    now = timezone.now()
    if lab is not None:
        base = {"total_terpenes": None, "terpenes": [], "cbd_total": None, "thc_total": None,
                "minor_cannabinoids": [], "tested_date": None, "lab_name": None, "coa_url": None, "contaminants": {}}
        BatchLab.objects.create(batch_id=p.batch_id, status="ok", checked_at=now, data={**base, **lab})
    if info is not None:
        ProductDetail.objects.create(product_id=p.product_id, status="ok", checked_at=now, data=info)
    return p


def _terps(**pcts):
    return [{"name": n.title(), "pct": v} for n, v in pcts.items()]


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class _Api(TestCase):
    @classmethod
    def setUpTestData(cls):
        # a fresh sync, so the staleness guard never tries to reach a broker
        SyncState.objects.update_or_create(location_slug=LOC, defaults={"last_synced_at": timezone.now()})

    def post(self, path, body):
        r = Client().post(f"/api/v1/{path}", data=json.dumps(body), content_type="application/json", **AUTH)
        self.assertEqual(r.status_code, 200, (path, r.content[:300]))
        return r.json()

    def search(self, slots, limit=20, offset=0, **extra):
        return self.post("products/search/", {"slots": {"store": LOC, **slots}, "limit": limit,
                                              "offset": offset, **extra})

    def skus(self, slots, **kw):
        return {r["sku"] for r in self.search(slots, **kw)["results"]}


# ── pure derivations ─────────────────────────────────────────────────────────
class ConcentrateKindsFromNames(TestCase):
    def _kinds(self, name, category="concentrates", strain="", info=None):
        p = Product(name=name, category=category, strain=strain)
        return ranking.kinds_of(p, info)

    def test_live_hash_rosin_answers_to_every_rosin_kind_and_its_texture(self):
        k = self._kinds("Fresh Bros Live Hash Rosin Badder 1g")
        self.assertTrue({"live-rosin", "hash-rosin", "rosin", "badder"} <= k, k)
        self.assertNotIn("live-resin", k)

    def test_cured_resin_is_its_own_kind_and_still_under_legacy_live_resin(self):
        k = self._kinds("Cured Resin Sauce 1g")
        self.assertTrue({"cured-resin", "live-resin", "sauce"} <= k, k)
        self.assertNotIn("rosin", k)

    def test_plain_rosin_is_not_live_or_hash_rosin(self):
        k = self._kinds("Rosin Jam 1g")
        self.assertIn("rosin", k)
        self.assertFalse({"live-rosin", "hash-rosin"} & k)

    def test_tags_name_the_extraction_when_the_name_does_not(self):
        self.assertIn("live-resin", self._kinds("Dabstract Cart High Life 1g", "vape-cartridges",
                                                info={"tags": ["1g", "Live Resin"]}))

    def test_a_strain_name_is_never_read_as_an_extraction(self):
        p = Product(name="Hash Plant Pre-Roll 1g", category="pre-rolls", strain="Hash Plant")
        self.assertFalse(product_attrs.is_infused_preroll(p))
        self.assertEqual(product_attrs.infusion_kinds(p), set())
        # the v2 extraction reading cuts the strain out (the legacy first-match subtype is unchanged)
        self.assertEqual(product_attrs.extraction_kinds(
            Product(name="Diamond OG Shatter 1g", category="concentrates", strain="Diamond OG")), {"shatter"})

    def test_solventless(self):
        self.assertTrue(product_attrs.is_solventless(Product(name="Ice Water Hash 1g", category="concentrates")))
        self.assertTrue(product_attrs.is_solventless(Product(name="Live Rosin Gummies 100mg", category="edibles")))
        self.assertFalse(product_attrs.is_solventless(Product(name="Live Resin Sauce 1g", category="concentrates")))
        self.assertFalse(product_attrs.is_solventless(Product(name="Blue Dream 3.5g", category="flower")))

    def test_master_split(self):
        def m(name, cat, info=None):
            return ranking.master_of(Product(name=name, category=cat), info)
        self.assertEqual(m("Live Resin Disposable Cartridge 1g", "vape-cartridges"), "Disposable Vape")
        self.assertEqual(m("Some AIO 1g", "vape-cartridges", {"ecom_subcategory": "disposables"}), "Disposable Vape")
        self.assertEqual(m("Distillate Cartridge 1g", "vape-cartridges"), "Vape Cartridge")
        self.assertEqual(m("Diamond Infused Pre-Roll 1g", "pre-rolls"), "Infused Pre-roll")
        self.assertEqual(m("Classic Pre-Roll 5pk", "pre-rolls"), "Pre-roll")
        self.assertEqual(m("Lemon Seltzer 10mg", "edibles"), "Liquid Edible")
        self.assertEqual(m("Berry Gummies 100mg", "edibles"), "Solid Edible")


# ── categories ───────────────────────────────────────────────────────────────
class MasterCategoriesTests(_Api):
    def setUp(self):
        _p("Blue Dream 3.5g", "flower")
        _p("Classic Pre-Roll 1g", "pre-rolls")
        _p("Diamond Infused Pre-Roll 1g", "pre-rolls")
        _p("Live Resin Sauce 1g", "concentrates")
        _p("Distillate Cartridge 1g", "vape-cartridges")
        _p("Live Resin Disposable 1g", "vape-cartridges")
        _p("Plain Cart 1g", "vape-cartridges", info={"ecom_subcategory": "disposables"})
        _p("Berry Gummies 100mg", "edibles")
        _p("Lemon Seltzer 10mg", "edibles")
        _p("Relief Balm", "topicals")
        _p("Sold Out Tincture", "tinctures", qty=0)            # out of stock: no Tincture entry at all
        _p("Hidden Capsules", "capsules", available=False)      # off the menu: no Capsule entry

    def test_master_list_in_order_with_counts_and_no_empty_master(self):
        res = Client().get(f"/api/v1/products/categories?store={LOC}", **AUTH).json()
        masters = [c["master"] for c in res["categories"]]
        self.assertEqual(masters, list(product_attrs.MASTER_ORDER))
        by = {c["master"]: c for c in res["categories"]}
        self.assertEqual(by["Disposable Vape"]["count"], 2)
        self.assertEqual(by["Vape Cartridge"]["count"], 1)
        self.assertEqual(by["Infused Pre-roll"]["value"], "infused-pre-rolls")
        self.assertFalse(res["skip"])
        for c in res["categories"]:
            self.assertGreaterEqual(c["count"], 1)

    def test_each_value_searches_to_exactly_its_count(self):
        res = self.post("products/categories", {"store": LOC})
        for c in res["categories"]:
            out = self.search({"category": c["value"]})
            self.assertEqual(out["total_matching"], c["count"], c)
            self.assertEqual(len(out["results"]), c["count"], c)

    def test_legacy_catalog_slugs_still_cover_the_whole_category(self):
        self.assertEqual(len(self.skus({"category": "vape-cartridges"})), 3)
        self.assertEqual(len(self.skus({"category": "pre-rolls"})), 2)
        self.assertEqual(len(self.skus({"category": "edibles"})), 2)

    def test_one_category_store_skips_the_step(self):
        Product.objects.exclude(category="flower").delete()
        self.assertTrue(self.post("products/categories", {"store": LOC})["skip"])


# ── stock gate ───────────────────────────────────────────────────────────────
class NoOutOfStockLeakTests(_Api):
    def setUp(self):
        lab = {"thc_total": 25.0, "terpenes": _terps(myrcene=1.0), "total_terpenes": 2.0,
               "minor_cannabinoids": [{"name": "CBG", "pct": 1.0}]}
        self.ok = _p("Good Rosin 1g", "concentrates", lab=lab, strain="Good")
        _p("Zero Rosin 1g", "concentrates", qty=0, lab=lab, sku="ZERO", strain="Good")
        _p("Thin Rosin 1g", "concentrates", qty=MIN_STOCK - 1, lab=lab, sku="THIN", strain="Good")
        _p("Gone Rosin 1g", "concentrates", available=False, lab=lab, sku="GONE", strain="Good")
        _p("Live Sold Rosin 1g", "concentrates", lab=lab, sku="LIVESOLD", strain="Good")
        self.bad = {"ZERO", "THIN", "GONE"}

    def test_search_facets_and_similar_never_see_out_of_stock(self):
        slots_list = [{"category": "concentrates"}, {"subcategory": "rosin"}, {"terpenes": ["myrcene"]},
                      {"cbg_min": 0.5}, {"q": "rosin"}, {"solventless": True}, {"thc_min": 20}]
        for slots in slots_list:
            out = self.search(slots)
            self.assertFalse({r["sku"] for r in out["results"]} & self.bad, slots)
            self.assertEqual(out["total_matching"], 2, slots)
        for name in facets.FACET_NAMES:
            res = self.post("products/facets", {"store": LOC, "category": "concentrates", "facet": name})
            for o in res["options"]:
                self.assertLessEqual(o["count"], 2, (name, o))
        sub = self.post("products/subtypes", {"slots": {"store": LOC, "category": "concentrates"}})
        self.assertEqual({o["value"]: o["count"] for o in sub["subtypes"]}["rosin"], 2)
        sim = self.post("products/similar", {"store": LOC, "sku": self.ok.sku})
        self.assertEqual({r["sku"] for r in sim["results"]}, {"LIVESOLD"})

    def test_the_live_pull_vetoes_a_sellout_everywhere(self):
        # the live pull is the stock authority when usable: it agrees with the table except LIVESOLD
        rows = [{"sku": p.sku, "product_id": p.product_id, "name": p.name, "price": 30,
                 "quantity_on_hand": 0.0 if p.sku == "LIVESOLD" else float(p.quantity_on_hand)}
                for p in Product.objects.all()]
        live_stock.prime(LOC, rows)
        self.assertEqual(self.skus({"category": "concentrates"}), {self.ok.sku})
        cats = self.post("products/categories", {"store": LOC})["categories"]
        self.assertEqual(cats[0]["count"], 1)
        self.assertEqual(self.post("products/similar", {"store": LOC, "sku": self.ok.sku})["results"], [])


# ── paging ───────────────────────────────────────────────────────────────────
class OffsetPagingTests(_Api):
    def setUp(self):
        for i in range(26):
            _p(f"Strain {i} Flower 3.5g", "flower", price=20 + i, brand=f"B{i % 4}", unit_weight=3.5)

    def test_pages_are_disjoint_stable_and_capped_at_20(self):
        full = [r["sku"] for r in self.search({"category": "flower"}, limit=20)["results"]]
        self.assertEqual(len(full), SEARCH_CAP)
        seen = []
        for offset in (0, 5, 10, 15):
            out = self.search({"category": "flower"}, limit=5, offset=offset)
            self.assertEqual(out["total_matching"], 20)
            self.assertEqual(out["has_more"], offset < 15)
            self.assertEqual([r["rank"] for r in out["results"]], list(range(offset + 1, offset + 6)))
            seen += [r["sku"] for r in out["results"]]
        self.assertEqual(seen, full)          # same order, no repeat, nothing skipped
        again = [r["sku"] for r in self.search({"category": "flower"}, limit=5, offset=5)["results"]]
        self.assertEqual(again, full[5:10])   # stable across calls

    def test_beyond_the_cap_is_empty_and_overlong_pages_are_clipped(self):
        out = self.search({"category": "flower"}, limit=5, offset=20)
        self.assertEqual((out["results"], out["has_more"]), ([], False))
        out = self.search({"category": "flower"}, limit=10, offset=15)
        self.assertEqual(len(out["results"]), 5)
        self.assertFalse(out["has_more"])

    def test_short_list_reports_its_true_total(self):
        out = self.search({"category": "flower", "price_max": 24}, limit=5)
        self.assertEqual(out["total_matching"], 5)
        self.assertFalse(out["has_more"])
        out = self.search({"category": "flower", "price_max": 27}, limit=5)
        self.assertEqual(out["total_matching"], 8)
        self.assertTrue(out["has_more"])
        self.assertEqual(len(self.search({"category": "flower", "price_max": 27}, limit=5, offset=5)["results"]), 3)


# ── every new slot narrows ───────────────────────────────────────────────────
class NewSlotsNarrowTests(_Api):
    def setUp(self):
        self.a = _p("Alpha Live Rosin Badder 1g", "concentrates", strain="Alpha",
                    lab={"thc_total": 78.0, "terpenes": _terps(myrcene=2.5, limonene=1.0), "total_terpenes": 4.0,
                         "minor_cannabinoids": [{"name": "CBG", "pct": 1.6}, {"name": "CBN", "pct": 0.2}]},
                    info={"tags": ["Solventless", "Small Batch"]})
        self.b = _p("Beta Live Resin Sauce 1g", "concentrates", strain="Beta",
                    lab={"thc_total": 85.0, "terpenes": _terps(limonene=3.0), "total_terpenes": 5.5,
                         "cbd_total": 2.0, "minor_cannabinoids": [{"name": "CBN", "pct": 0.9}]})
        self.c = _p("Gamma Distillate Syringe 1g", "concentrates", strain="Gamma", thc_percent=92.0)
        self.d = _p("Delta Cured Resin Sugar 1g", "concentrates", strain="Delta",
                    lab={"thc_total": 70.0, "terpenes": _terps(pinene=0.8), "total_terpenes": 1.2,
                         "minor_cannabinoids": [{"name": "THCV", "pct": 0.5}, {"name": "CBC", "pct": 0.7}]})
        self.ip1 = _p("Kief Infused Pre-Roll 1g", "pre-rolls", strain="Eps")
        self.ip2 = _p("Diamond Infused Pre-Roll 2pk", "pre-rolls", strain="Zeta")
        self.pr = _p("Classic Pre-Roll 5pk", "pre-rolls", strain="Eta")
        self.pr1 = _p("Classic Pre-Roll 1g", "pre-rolls", strain="Theta")

    def S(self, **slots):
        return self.skus({"category": "concentrates", **slots})

    def test_each_slot(self):
        a, b, c, d = self.a.sku, self.b.sku, self.c.sku, self.d.sku
        self.assertEqual(self.S(), {a, b, c, d})
        self.assertEqual(self.S(thc_min=80), {b, c})                     # lab THC, else inventory THC
        self.assertEqual(self.S(thc_min=75, thc_max=86), {a, b})
        self.assertEqual(self.S(cbd_min=1), {b})
        self.assertEqual(self.S(cbg_min=1), {a})
        self.assertEqual(self.S(cbn_min=0.5), {b})
        self.assertEqual(self.S(cbc_min=0.5), {d})
        self.assertEqual(self.S(thcv_min=0.4), {d})
        self.assertEqual(self.S(terpenes=["Beta-Myrcene"]), {a})
        self.assertEqual(self.S(terpenes=["myrcene", "pinene"]), {a, d})  # ANY
        self.assertEqual(self.S(terpene_total_min=4), {a, b})
        self.assertEqual(self.S(lab_tested=True), {a, b, d})
        self.assertEqual(self.S(tags=["small batch"]), {a})
        self.assertEqual(self.S(tags=["sauce"]), {b})                    # a whole phrase of the name
        self.assertEqual(self.S(q="beta sauce"), {b})                    # ALL tokens
        self.assertEqual(self.S(q="rosin"), {a})
        self.assertEqual(self.S(subcategory="live-rosin"), {a})
        self.assertEqual(self.S(subcategory="cured-resin"), {d})
        self.assertEqual(self.S(subcategory="live-resin"), {b, d})       # legacy fold: cured resin is live resin
        self.assertEqual(self.S(subcategory="rosin"), {a})               # legacy: rosin covers live rosin
        self.assertEqual(self.S(solventless=True), {a})
        self.assertEqual(self.S(terpenes=["limonene"], thc_min=80), {b})  # slots combine (AND)

    def test_terpenes_rank_by_amount(self):
        out = self.search({"category": "concentrates", "terpenes": ["limonene"], "sort_by": "price_asc"})
        self.assertEqual({r["sku"] for r in out["results"]}, {self.a.sku, self.b.sku})

    def test_infusion_pack_and_infused_master(self):
        self.assertEqual(self.skus({"category": "pre-rolls", "infusion": "kief"}), {self.ip1.sku})
        self.assertEqual(self.skus({"category": "pre-rolls", "infusion": ["diamonds"]}), {self.ip2.sku})
        self.assertEqual(self.skus({"category": "infused-pre-rolls"}), {self.ip1.sku, self.ip2.sku})
        self.assertEqual(self.skus({"category": "regular-pre-rolls"}), {self.pr.sku, self.pr1.sku})
        self.assertEqual(self.skus({"category": "pre-rolls", "pack": 5}), {self.pr.sku})
        self.assertEqual(self.skus({"category": "pre-rolls", "size": "5pk"}), {self.pr.sku})   # no fill
        self.assertEqual(self.skus({"category": "pre-rolls", "pack": "single"}), {self.ip1.sku, self.pr1.sku})

    def test_garbage_slots_are_ignored_not_errors(self):
        out = self.search({"category": "concentrates", "thc_min": "lots", "terpenes": {"x": 1}, "q": 7,
                           "tags": [None, 3], "cbg_min": True, "pack": "huge", "solventless": "maybe"})
        self.assertEqual(len(out["results"]), 4)


# ── options never lead to zero, and their counts are what search finds ──────
class ViabilityTests(_Api):
    def setUp(self):
        NewSlotsNarrowTests.setUp(self)
        _p("Omega Shatter 1g", "concentrates", price=80, lab={"thc_total": 88.0, "terpenes": _terps(myrcene=0.4)})
        _p("Big DOH Shatter 1g", "concentrates", price=55)
        _p("Blue Dream 3.5g", "flower", unit_weight=3.5,
           lab={"thc_total": 27.0, "terpenes": _terps(myrcene=1.1, caryophyllene=0.5), "total_terpenes": 2.2})
        _p("OG 7g", "flower", unit_weight=7, thc_percent=19.0)

    def _assert_options_viable(self, base_slots, options, exact=True):
        for o in options:
            self.assertGreaterEqual(o["count"], 1, o)
            slots = {**base_slots}
            for k, v in o["slots"].items():
                slots[k] = v
            out = self.search(slots)
            self.assertGreaterEqual(out["total_matching"], 1, (base_slots, o))
            # a GRAM size may fill with the nearest weight (legacy soft fill, inside every hard filter)
            gram_size = str(o["slots"].get("size") or "").endswith("g")
            if exact and not gram_size:
                self.assertEqual(out["total_matching"], min(o["count"], SEARCH_CAP), (base_slots, o))

    def test_every_facet_option_leads_to_exactly_its_count(self):
        for base in ({"category": "concentrates"}, {"category": "concentrates", "subcategory": "rosin"},
                     {"category": "concentrates", "terpenes": ["limonene"]}, {"category": "flower"},
                     {"category": "pre-rolls"}, {"category": "concentrates", "doh_only": True}):
            for name in facets.FACET_NAMES:
                res = self.post("products/facets", {"store": LOC, "slots": base, "facet": name})
                # list facets ignore their own slot and append (ANY), so their option counts are exact only
                # when that list was empty
                exact = not (name in ("terpenes", "tags") and base.get(name))
                self._assert_options_viable(base, res["options"], exact=exact)
                if res["skip"]:
                    continue
                self.assertTrue(res["options"], (base, name))

    def test_step_endpoints_honour_all_slots_and_never_list_zero(self):
        base = {"store": LOC, "category": "concentrates", "thc_min": 80}
        sub = self.post("products/subtypes", {"slots": base})
        self.assertNotIn("live-rosin", {o["value"] for o in sub["subtypes"]})   # 78% THC: filtered out
        self._assert_options_viable(base, sub["options"])
        bands = self.post("products/price-bands", {"slots": base})
        self.assertIn("count", bands)
        self._assert_options_viable(base, [b for b in bands["bands"] if "min" in b])
        doh = self.post("products/doh-options", {"slots": base})
        self.assertEqual((doh["doh"], doh["non_doh"]), (0, 3))
        self.assertTrue(doh["skip"])
        self.assertEqual(doh["options"], [])
        sizes = self.post("products/sizes", {"slots": {"store": LOC, "category": "flower"}})
        self.assertEqual([s["value"] for s in sizes["sizes"]], ["3.5g", "7g"])
        self._assert_options_viable({"category": "flower"}, sizes["options"])
        one = self.post("products/sizes", {"slots": {"store": LOC, "category": "flower", "thc_min": 25}})
        self.assertEqual(([s["value"] for s in one["sizes"]], one["skip"]), (["3.5g"], True))

    def test_get_query_string_twin(self):
        r = Client().get(f"/api/v1/products/subtypes?store={LOC}&category=concentrates&terpenes=myrcene"
                         "&terpenes=pinene", **AUTH).json()
        self.assertEqual({o["value"] for o in r["subtypes"]} >= {"live-rosin", "cured-resin", "shatter"}, True)

    def test_specify_more_groups_are_non_empty_and_viable(self):
        res = self.post("products/specify-more", {"store": LOC, "category": "concentrates", "slots": {}})
        names = [g["facet"] for g in res["groups"]]
        self.assertIn("extraction", names)
        self.assertIn("terpenes", names)
        for g in res["groups"]:
            self.assertTrue(g["options"])
            self._assert_options_viable({"category": "concentrates"}, g["options"])
        topicals = self.post("products/specify-more", {"store": LOC, "category": "topicals", "slots": {}})
        self.assertEqual((topicals["groups"], topicals["skip"]), ([], True))

    def test_unknown_facet_is_a_400(self):
        r = Client().post("/api/v1/products/facets", data=json.dumps({"store": LOC, "facet": "cost"}),
                          content_type="application/json", **AUTH)
        self.assertEqual(r.status_code, 400)


# ── soft fallback stays inside the hard filters ──────────────────────────────
class SoftFallbackNoLeakTests(_Api):
    def test_nearest_weight_fill_respects_category_doh_and_v2_slots(self):
        _p("DOH Blue 3.5g", "flower", unit_weight=3.5, thc_percent=30.0)
        _p("DOH Green 7g", "flower", unit_weight=7, thc_percent=31.0)
        _p("Plain Red 7g", "flower", unit_weight=7, thc_percent=32.0)            # not DOH
        _p("DOH Weak 7g", "flower", unit_weight=7, thc_percent=15.0)             # below thc_min
        _p("DOH Cart 1g", "vape-cartridges", unit_weight=3.5, thc_percent=90.0)  # wrong category
        got = self.skus({"category": "flower", "size": "3.5g", "doh_only": True, "thc_min": 25}, limit=5)
        names = set(Product.objects.filter(sku__in=got).values_list("name", flat=True))
        self.assertEqual(names, {"DOH Blue 3.5g", "DOH Green 7g"})


# ── find similar ─────────────────────────────────────────────────────────────
class FindSimilarTests(_Api):
    def setUp(self):
        lab = {"thc_total": 24.0, "terpenes": _terps(myrcene=1.2, limonene=0.4), "total_terpenes": 2.0,
               "coa_url": "https://certs.example.com/x.pdf"}
        self.anchor = _p("Blue Dream 3.5g", "flower", strain="Blue Dream", unit_weight=3.5, lab=lab, qty=0)
        self.same1 = _p("Blue Dream Smalls 3.5g", "flower", strain="Blue Dream", unit_weight=3.5, lab=lab,
                        info={"tags": ["Indoor"]})
        self.same2 = _p("Blue Dream 7g", "flower", strain="Blue Dream", unit_weight=7)
        self.other = _p("OG Kush 3.5g", "flower", strain="OG Kush", unit_weight=3.5, lab=lab)
        _p("Blue Dream Cart 1g", "vape-cartridges", strain="Blue Dream")         # other category
        _p("Blue Dream Gone 3.5g", "flower", strain="Blue Dream", qty=1)          # out of stock

    def test_full_cards_with_lab_same_strain_first_in_stock_only(self):
        res = self.post("products/similar", {"store": LOC, "sku": self.anchor.sku})
        skus = [r["sku"] for r in res["results"]]
        self.assertEqual(set(skus[:2]), {self.same1.sku, self.same2.sku})
        self.assertEqual(set(skus), {self.same1.sku, self.same2.sku, self.other.sku})
        first = next(r for r in res["results"] if r["sku"] == self.same1.sku)
        self.assertEqual(first["lab"]["terpenes"][0]["name"], "Myrcene")
        self.assertEqual(first["info"], {"tags": ["Indoor"]})
        from budtender.serializers import PUBLIC_PRODUCT_FIELDS
        for r in res["results"]:
            self.assertEqual(set(r), set(PUBLIC_PRODUCT_FIELDS))
        self.assertNotIn("margin", json.dumps(res).lower())
        self.assertNotIn("cost", json.dumps(res).lower())
        self.assertEqual(res["total_matching"], 3)

    def test_anchor_by_website_slug_and_paging(self):
        res = self.post("products/similar", {"store": LOC, "slug": "blue-dream-35g", "limit": 2})
        self.assertEqual(res["anchor"]["sku"], self.anchor.sku)
        self.assertEqual(len(res["results"]), 2)
        self.assertTrue(res["has_more"])
        nxt = self.post("products/similar", {"store": LOC, "slug": "blue-dream-35g", "limit": 2, "offset": 2})
        self.assertEqual([r["sku"] for r in nxt["results"]], [self.other.sku])

    def test_unknown_anchor(self):
        self.assertEqual(self.post("products/similar", {"store": LOC, "sku": "nope"})["results"], [])


@override_settings(HHT_BACKEND_TOKEN="backend", HHT_WEBSITE_TOKEN="website")
class WebsiteTokenOpensSearchV2(TestCase):
    def test_website_token(self):
        for path in ("products/categories", "products/facets", "products/specify-more", "products/similar"):
            r = Client().post(f"/api/v1/{path}", data=json.dumps({"store": LOC, "facet": "thc"}),
                              content_type="application/json", HTTP_AUTHORIZATION="Bearer website")
            self.assertEqual(r.status_code, 200, path)
            r = Client().post(f"/api/v1/{path}", data="{}", content_type="application/json",
                              HTTP_AUTHORIZATION="Bearer nope")
            self.assertIn(r.status_code, (401, 403), path)


class SharedProfileNeverPersonalisesSearch(_Api):
    def test_a_shared_row_linked_to_the_session_is_not_used(self):
        from unittest import mock
        shared = CustomerProfile.objects.create(phone="+15095550000", dutchie_ids=["1", "2", "3", "4"])
        ChatSession.objects.create(session_token="tok-shared", customer=shared, location_slug=LOC)
        with mock.patch("budtender.views.rank_products", return_value=[]) as rank:
            self.search({"category": "flower"}, session_token="tok-shared")
        self.assertIsNone(rank.call_args.args[2])
