"""customer_model.compute_derived: the contract's `derived` block (docs/contracts/customer-memory-v1.md),
computed from purchase history joined to the catalogue, deterministic, no LLM. Synthetic personas only
(tailoring_fixtures)."""
import json
from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from budtender import customer_model, product_attrs
from budtender.models import ChatSession, Product, SuggestedProduct
from budtender.tests import tailoring_fixtures as fx

CONTRACT_KEYS = {"ratio_pref", "cbd_lean", "forms", "extraction", "dose_mg", "price_by_cat", "thc_by_cat",
                 "cadence_days", "days_since_last", "due_for_reorder", "pairings", "next_likely", "confidence"}


class ProductDerivations(SimpleTestCase):
    def P(self, name, category="edibles", **kw):
        return Product(name=name, category=category, **kw)

    def test_ratio_from_the_name_in_thc_cbd_order(self):
        r = product_attrs.cannabinoid_ratio
        self.assertEqual(r(self.P("Verdelux 1:1 Gummies 10mg 10pk")), "1:1")
        self.assertEqual(r(self.P("Calm 2:1 THC:CBD Gummies")), "2:1")
        self.assertEqual(r(self.P("Calm 2:1 CBD:THC Gummies")), "1:2")          # printed CBD first -> flipped
        self.assertEqual(r(self.P("Hemp Hollow 20:1 CBD Gummies 10pk", strain_type="cbd")), "1:20")
        self.assertEqual(r(self.P("Sleepy 1:1:1 THC:CBD:CBN Gummies")), "1:1:1")
        # the lab decides the orientation when the label does not
        self.assertEqual(r(self.P("Elixir 4:1 Tincture", "tinctures"), lab={"thc_total": 50, "cbd_total": 200}), "1:4")
        self.assertEqual(r(self.P("Elixir 4:1 Tincture", "tinctures"), lab={"thc_total": 200, "cbd_total": 50}), "4:1")

    def test_ratio_from_the_lab_only_when_cbd_is_a_real_share(self):
        r = product_attrs.cannabinoid_ratio
        self.assertEqual(r(self.P("Balance Gummies"), lab={"thc_total": 100, "cbd_total": 98}), "1:1")
        self.assertEqual(r(self.P("Calm Gummies"), lab={"thc_total": 50, "cbd_total": 100}), "1:2")
        self.assertIsNone(r(self.P("Blue Dream 3.5g", "flower"), lab={"thc_total": 24, "cbd_total": 0.6}))
        self.assertIsNone(r(self.P("Sour Blast THC Gummies 100mg 10pk")))
        self.assertIsNone(r(self.P("Gelato 33 3.5g", "flower")))
        self.assertIsNone(r(self.P("Wedding Cake 4:20 Pack 3.5g", "flower")))   # no cannabinoid, not an edible

    def test_forms(self):
        f = product_attrs.edible_form
        self.assertEqual(f(self.P("Berry Gummies 100mg")), "gummy")
        self.assertEqual(f(self.P("Dark Chocolate Bar 100mg")), "chocolate")
        self.assertEqual(f(self.P("Lemon Seltzer 10mg")), "drink")
        self.assertEqual(f(self.P("Balance 1:1 Tincture 300mg", "tinctures")), "tincture")
        self.assertEqual(f(self.P("CBD Balm", "topicals")), "topical")
        self.assertEqual(f(self.P("Peppermint Mints 100mg")), "mint")
        self.assertIsNone(f(self.P("Blue Dream 3.5g", "flower")))

    def test_extraction_methods_and_full_spectrum(self):
        m = product_attrs.extraction_methods
        self.assertEqual(m(self.P("Live Hash Rosin Badder 1g", "concentrates")), {"live-rosin", "hash-rosin", "rosin"})
        self.assertEqual(product_attrs.primary_methods({"live-rosin", "hash-rosin", "rosin"}), {"live-rosin", "hash-rosin"})
        self.assertEqual(m(self.P("Live Resin Sauce 1g", "concentrates")), {"live-resin"})
        self.assertEqual(m(self.P("Distillate Cartridge 1g", "vape-cartridges")), {"distillate"})
        self.assertEqual(m(self.P("Live Rosin Gummies 100mg")), {"live-rosin", "rosin"})
        self.assertEqual(m(self.P("Full Spectrum 1:1 Gummies 10mg")), {"full-spectrum"})
        self.assertEqual(m(self.P("FSO Capsules 10mg", "capsules")), {"full-spectrum"})
        self.assertEqual(m(self.P("Glass Shatter 1g", "concentrates")), set())      # a texture, not a method
        self.assertEqual(m(self.P("Hash Plant 3.5g", "flower", strain="Hash Plant")), set())

    def test_per_piece_mg(self):
        mg = product_attrs.piece_mg
        self.assertEqual(mg(self.P("Gummies 10mg 10pk")), 10)
        self.assertEqual(mg(self.P("Gummies 100mg 10pk")), 10)
        self.assertEqual(mg(self.P("Midnight Gummies 5mg 20pk")), 5)
        self.assertEqual(mg(self.P("Lemon Seltzer 10mg")), 10)
        self.assertEqual(mg(self.P("Gummies 100mg (10mg x 10)")), 10)
        self.assertIsNone(mg(self.P("Dark Chocolate Bar 100mg")))             # package total, no count
        self.assertIsNone(mg(self.P("1:1 Tincture 300mg", "tinctures")))
        self.assertIsNone(mg(self.P("Blue Dream 3.5g", "flower")))
        self.assertEqual(mg(self.P("CBD Gummies 10pk", potency_mg=100)), 10)


class _Personas(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.cat, cls.p = fx.make_all()

    def d(self, key):
        return customer_model.compute_derived(self.p[key])


class DerivedPerPersona(_Personas):
    def test_schema_is_exactly_the_contract_and_small(self):
        for key in fx.HISTORY:
            d = self.d(key)
            self.assertEqual(set(d), CONTRACT_KEYS, key)
            self.assertEqual(set(d["pairings"]), {"accepted", "declined"})
            self.assertIn(d["confidence"], ("low", "med", "high"))
            self.assertLess(len(json.dumps(d)), 1500, key)   # memory's whole budget is 4 KB
            self.assertEqual(d, self.d(key), "deterministic")

    def test_ratio_gummy_buyer(self):
        d = self.d("ratio")
        self.assertEqual(d["ratio_pref"], ["1:1", "2:1"])
        self.assertEqual(d["cbd_lean"], 1)
        self.assertEqual(max(d["forms"], key=d["forms"].get), "gummy")
        self.assertEqual(d["forms"]["gummy"], 0.83)
        self.assertEqual(d["dose_mg"], {"min": 5, "p50": 10, "max": 10})
        self.assertEqual(d["price_by_cat"]["edibles"], {"p10": 20, "p50": 21, "p90": 22})
        self.assertNotIn("edibles", d["thc_by_cat"])     # a gummy's THC "%" is a package mg: never a band
        self.assertEqual(d["next_likely"][0], "edibles")
        self.assertEqual((d["cadence_days"], d["days_since_last"], d["due_for_reorder"]), (11, 6, False))
        self.assertEqual(d["confidence"], "high")

    def test_flower_connoisseur(self):
        d = self.d("conn")
        self.assertEqual(d["price_by_cat"]["flower"], {"p10": 55, "p50": 58, "p90": 60})
        self.assertEqual(d["thc_by_cat"]["flower"], {"p10": 29, "p50": 31, "p90": 32})
        self.assertEqual(d["ratio_pref"], [])
        self.assertEqual(d["cbd_lean"], 0)
        self.assertEqual(d["pairings"]["accepted"], ["flower|pre-rolls"])   # bought together the same day
        self.assertEqual(d["next_likely"], ["flower", "pre-rolls"])
        self.assertEqual(d["confidence"], "med")

    def test_concentrate_explorer(self):
        d = self.d("rosin")
        self.assertEqual(d["extraction"], {"live-rosin": 0.86, "hash-rosin": 0.29, "live-resin": 0.14})
        self.assertEqual(d["price_by_cat"]["concentrates"], {"p10": 30, "p50": 50, "p90": 60})
        self.assertEqual(d["next_likely"][:2], ["concentrates", "vape-cartridges"])

    def test_budget_shopper(self):
        d = self.d("budget")
        self.assertEqual(d["price_by_cat"]["flower"], {"p10": 15, "p50": 15, "p90": 18})
        self.assertEqual(d["price_by_cat"]["pre-rolls"]["p90"], 8)
        self.assertEqual(d["dose_mg"], {"min": 5, "p50": 5, "max": 5})
        self.assertEqual(d["next_likely"][0], "flower")

    def test_new_customer_is_empty_and_low(self):
        self.assertEqual(self.d("new"), customer_model.EMPTY)
        self.assertIsNone(customer_model.tailor_for(self.p["new"]))
        self.assertIsNone(customer_model.tailor_for(None))

    def test_two_orders_is_low_confidence_so_no_tailoring(self):
        self.assertEqual(self.d("low")["confidence"], "low")
        self.assertIsNone(customer_model.tailor_for(self.p["low"]))
        self.assertIsNotNone(customer_model.tailor_for(self.p["ratio"]))

    def test_a_product_gone_from_the_catalogue_still_counts_from_its_own_line(self):
        prof = self.p["ratio"]
        hist = list(prof.purchase_history) + [{
            "product_id": "gone-1", "sku": "GONE1", "product_name": "Retired 1:1 Gummies 10mg 10pk",
            "category": "edibles", "times_bought": 1, "last_price": 19.0,
            "first_bought_at": (timezone.now() - timedelta(days=90)).isoformat(),
            "last_bought_at": (timezone.now() - timedelta(days=90)).isoformat()}]
        prof.purchase_history = hist
        rows = customer_model.history_rows(prof)
        gone = [r for r in rows if r["sku"] == "GONE1"][0]
        self.assertFalse(gone["in_catalogue"])
        self.assertEqual((gone["ratio"], gone["form"], gone["piece_mg"]), ("1:1", "gummy", 10))

    def test_reorder_is_due_at_85_percent_of_the_usual_gap(self):
        self.assertFalse(customer_model.due_for_reorder(14, 11))
        self.assertTrue(customer_model.due_for_reorder(14, 12))
        self.assertFalse(customer_model.due_for_reorder(None, 40))


class PairingsFromSuggestions(_Personas):
    def test_a_shown_pairing_bought_later_is_accepted_one_never_bought_is_declined(self):
        prof = self.p["conn"]
        s = ChatSession.objects.create(session_token="s-pairing-test-0001", customer=prof)
        now = timezone.now()
        # shown before they last bought PR2 (15 days ago) -> accepted
        a = SuggestedProduct.objects.create(session=s, customer=prof, location_slug=fx.LOC, sku="PR2",
                                            kind="pairing", paired_with_sku="F6")
        # an edible shown with flower 30 days ago, never bought -> declined
        b = SuggestedProduct.objects.create(session=s, customer=prof, location_slug=fx.LOC, sku="E4",
                                            kind="pairing", paired_with_sku="F5")
        SuggestedProduct.objects.filter(pk=a.pk).update(shown_at=now - timedelta(days=20))
        SuggestedProduct.objects.filter(pk=b.pk).update(shown_at=now - timedelta(days=30))
        d = customer_model.compute_derived(prof)
        self.assertEqual(d["pairings"], {"accepted": ["flower|pre-rolls"], "declined": ["flower|edibles"]})
        # a fresh suggestion (< 14 days) is neither yet
        SuggestedProduct.objects.filter(pk=b.pk).update(shown_at=now - timedelta(days=3))
        self.assertEqual(customer_model.compute_derived(prof)["pairings"]["declined"], [])


class StoredDerivedIsRead(_Personas):
    def test_memory_derived_wins_and_is_read_without_a_query(self):
        prof = self.p["ratio"]
        stored = {**customer_model.compute_derived(prof), "ratio_pref": ["1:20"]}
        prof.memory = {"v": 1, "derived": stored}
        with self.assertNumQueries(0):
            self.assertEqual(customer_model.derived_for(prof)["ratio_pref"], ["1:20"])

    def test_computed_once_per_profile_object(self):
        prof = self.p["rosin"]
        prof.memory = {}
        customer_model.derived_for(prof)
        with self.assertNumQueries(0):
            customer_model.derived_for(prof)

    def test_a_malformed_stored_derived_is_ignored(self):
        prof = self.p["ratio"]
        prof.memory = {"derived": {"ratio_pref": "nonsense"}}
        self.assertEqual(customer_model.derived_for(prof)["ratio_pref"], ["1:1", "2:1"])
