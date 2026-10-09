"""Tailored ranking + pairing: a med/high-confidence customer's `derived` (ratio, form, extraction, per-piece
mg, price/THC band per category, last purchase, pairing habit) is a SOFT signal on top of every hard filter.
Anonymous / new / low-confidence shoppers are locked to the pre-tailoring output by test_tailored_regression.
"""
import json
import re

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from budtender import customer_model, deals, product_attrs
from budtender.engine import MAX_PAIR_PRICE_RATIO, pair_for
from budtender.models import Product, SyncState
from budtender.ranking import MIN_STOCK, rank_products
from budtender.tests import tailoring_fixtures as fx

# LCB-CONTENT-COMPLIANCE.md §2.1 / §5 plus house rules: no effect-as-treatment words, no "we tracked you".
BANNED = re.compile(r"\b(?:treat\w*|relie\w*|medic\w*|therap\w*|heal\w*|cure|sleep|insomnia|anxiety|pain|dose\w*"
                    r"|dosage|tracked|tracking|history|data|margin|cost|free)\b", re.I)


class _T(TestCase):
    @classmethod
    def setUpTestData(cls):
        SyncState.objects.update_or_create(location_slug=fx.LOC, defaults={"last_synced_at": timezone.now()})
        cls.cat, cls.p = fx.make_all()

    def rank(self, who, slots, limit=5):
        prof = self.p[who] if who else None
        return rank_products(fx.LOC, {"store": fx.LOC, **slots}, prof, limit=limit)

    def band(self, who, key, cat):
        b = customer_model.derived_for(self.p[who])[key][cat]
        return b["p10"] * 0.75, b["p90"] * 1.25


class RatioGummyBuyer(_T):
    def test_ratio_products_lead_the_edibles_shelf(self):
        picks = self.rank("ratio", {"category": "edibles"})
        ratios = [product_attrs.cannabinoid_ratio(p) for p, _ in picks]
        self.assertTrue(all(r in ("1:1", "2:1") for r in ratios[:3]), ratios)
        # what the same shelf shows an anonymous shopper: margin-first, a THC-only gummy on top
        anon = self.rank(None, {"category": "edibles"})
        self.assertIsNone(product_attrs.cannabinoid_ratio(anon[0][0]))
        lo, hi = self.band("ratio", "price_by_cat", "edibles")
        self.assertTrue(all(lo <= float(p.price) <= hi for p, _ in picks[:3]), [(p.sku, p.price) for p, _ in picks])

    def test_the_ratio_gummy_leads_the_solid_edibles_master_too(self):
        p, why = self.rank("ratio", {"category": "solid-edibles"})[0]
        self.assertEqual(product_attrs.cannabinoid_ratio(p), "1:1")
        self.assertEqual(product_attrs.edible_form(p), "gummy")
        self.assertTrue(why.startswith("The ratio you usually pick"), why)

    def test_their_last_purchase_is_not_pushed_again_before_it_is_due(self):
        skus = [p.sku for p, _ in self.rank("ratio", {"category": "edibles"})]
        self.assertNotIn("E1", skus[:3])   # bought 6 days ago, they come every ~11

    def test_a_sold_out_favourite_never_comes_back(self):
        skus = {p.sku for p, _ in self.rank("ratio", {"category": "edibles"}, limit=20)}
        self.assertNotIn("E12", skus)       # they bought it twice; 2 on the floor < MIN_STOCK
        self.assertTrue(all(Product.objects.get(sku=s).quantity_on_hand >= MIN_STOCK for s in skus))

    def test_tinctures_pick_their_ratio(self):
        self.assertEqual(self.rank("ratio", {"category": "tinctures"})[0][0].sku, "T1")

    def test_pairing_with_flower_is_a_ratio_gummy_not_a_thc_only_one(self):
        anchor = self.cat["F5"]
        pair, code, text, _ = pair_for(fx.LOC, anchor, self.p["ratio"])
        self.assertIn(product_attrs.cannabinoid_ratio(pair), ("1:1", "2:1"))
        self.assertEqual(code, "your_usual")
        self.assertLessEqual(float(pair.price), MAX_PAIR_PRICE_RATIO * float(anchor.price))
        anon_pair = pair_for(fx.LOC, anchor, None)[0]
        self.assertIsNone(product_attrs.cannabinoid_ratio(anon_pair))


class FlowerConnoisseur(_T):
    def test_top_tier_high_thc_leads(self):
        picks = self.rank("conn", {"category": "flower"})
        top = picks[0][0]
        plo, phi = self.band("conn", "price_by_cat", "flower")
        tlo, thi = self.band("conn", "thc_by_cat", "flower")
        self.assertTrue(plo <= float(top.price) <= phi and tlo <= top.thc_percent <= thi, top.sku)
        self.assertEqual(top.sku, "F6")
        # anonymous: the $180 ounce (biggest margin) leads
        self.assertEqual(self.rank(None, {"category": "flower"})[0][0].sku, "F8")
        # the margin-king $40 / 20% eighth is out of their THC band: not in their top two
        self.assertNotIn("F7", [p.sku for p, _ in picks[:2]])

    def test_explicit_price_slot_is_never_relaxed(self):
        picks = self.rank("conn", {"category": "flower", "price_max": 32})
        self.assertTrue(picks)
        self.assertTrue(all(float(p.price) <= 32 for p, _ in picks))

    def test_explicit_size_and_category_never_relax(self):
        picks = self.rank("conn", {"category": "flower", "size": "28g"})
        self.assertEqual([p.sku for p, _ in picks], ["F8"])
        self.assertTrue(all(p.category == "pre-rolls" for p, _ in self.rank("conn", {"category": "pre-rolls"})))
        self.assertEqual(self.rank("conn", {"category": "flower", "doh_only": True}), [])

    def test_pairing_follows_their_pre_roll_habit(self):
        pair, code, _, _ = pair_for(fx.LOC, self.cat["F6"], self.p["conn"])
        self.assertEqual(pair.category, "pre-rolls")


class ConcentrateExplorer(_T):
    def test_live_rosin_leads_concentrates(self):
        p, why = self.rank("rosin", {"category": "concentrates"})[0]
        self.assertIn("live-rosin", product_attrs.extraction_methods(p))
        self.assertTrue(why.startswith("Live rosin, like you usually pick"), why)
        self.assertEqual(self.rank(None, {"category": "concentrates"})[1][0].sku, "C5")   # anon: distillate #2

    def test_just_bought_cart_is_not_first(self):
        self.assertNotEqual(self.rank("rosin", {"category": "vape-cartridges"})[0][0].sku, "V3")

    def test_subtype_slot_is_still_hard(self):
        picks = self.rank("rosin", {"category": "concentrates", "subcategory": "live-resin"})
        self.assertTrue(picks)
        self.assertTrue(all("live-resin" in product_attrs.extraction_methods(p) | {"live-resin"} and
                            "rosin" not in p.name.lower() for p, _ in picks))


class BudgetShopper(_T):
    def test_value_eighths_lead_and_the_ounce_stays_out_of_the_top(self):
        picks = self.rank("budget", {"category": "flower"})
        lo, hi = self.band("budget", "price_by_cat", "flower")
        self.assertTrue(lo <= float(picks[0][0].price) <= hi, picks[0][0].sku)
        self.assertNotIn("F8", [p.sku for p, _ in picks[:3]])
        self.assertEqual(self.rank(None, {"category": "flower"})[0][0].sku, "F8")

    def test_their_5mg_gummy_leads_edibles(self):
        p, why = self.rank("budget", {"category": "edibles"})[0]
        self.assertEqual(product_attrs.piece_mg(p), 5)


class CopyAndApi(_T):
    def test_every_tailored_reason_is_compliant_and_short(self):
        for who in ("ratio", "conn", "rosin", "budget"):
            for cat in ("flower", "edibles", "concentrates", "vape-cartridges", "pre-rolls", "tinctures"):
                for p, why in self.rank(who, {"category": cat}):
                    self.assertTrue(why and len(why) <= 90, (who, p.sku, why))
                    self.assertIsNone(BANNED.search(why), (who, p.sku, why))
                    self.assertNotIn("$", why.replace("save $", ""))
            for a in ("F5", "C1", "E1", "V3"):
                _, _, text, _ = pair_for(fx.LOC, self.cat[a], self.p[who])
                self.assertIsNone(BANNED.search(text or ""), (who, a, text))

    @override_settings(HHT_BACKEND_TOKEN="test-token")
    def test_search_api_tailors_for_the_caller_and_leaks_nothing(self):
        r = Client().post("/api/v1/products/search/", data=json.dumps({
            "slots": {"store": fx.LOC, "category": "edibles"}, "limit": 5, "phone": fx.PHONES["ratio"]}),
            content_type="application/json", HTTP_AUTHORIZATION="Bearer test-token")
        self.assertEqual(r.status_code, 200)
        res = r.json()["results"]
        self.assertIn(product_attrs.cannabinoid_ratio(Product.objects.get(sku=res[0]["sku"])), ("1:1", "2:1"))
        body = r.content.decode()
        for leak in ('"cost"', '"margin"', "margin_pct", "price_z", "velocity", "ratio_pref", "cbd_lean"):
            self.assertNotIn(leak, body)

    def test_tailoring_is_deterministic(self):
        a = [(p.sku, w) for p, w in self.rank("rosin", {"category": "concentrates"}, limit=8)]
        self.p["rosin"].__dict__.pop("_hht_derived", None)
        b = [(p.sku, w) for p, w in self.rank("rosin", {"category": "concentrates"}, limit=8)]
        self.assertEqual(a, b)


class DealsForCustomer(_T):
    def test_relevant_deals_first_stable_otherwise(self):
        ds = [{"id": 1, "title": "20% off eighths", "description": ""},
              {"id": 2, "title": "Gummy Tuesday", "description": "15% off all gummies"},
              {"id": 3, "title": "1:1 edibles 10% off", "description": ""},
              {"id": 4, "title": "Dab day", "description": "live rosin 20% off"}]
        ratio = customer_model.derived_for(self.p["ratio"])
        self.assertEqual([d["id"] for d in deals.for_customer(ds, ratio)][:2], [3, 2])
        rosin = customer_model.derived_for(self.p["rosin"])
        self.assertEqual(deals.for_customer(ds, rosin)[0]["id"], 4)
        self.assertEqual(deals.for_customer(ds, customer_model.EMPTY), ds)
        self.assertEqual(deals.for_customer(ds, None), ds)
        self.assertEqual(sorted(d["id"] for d in deals.for_customer(ds, ratio)), [1, 2, 3, 4])
