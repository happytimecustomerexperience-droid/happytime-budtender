"""The `aroma` slot (citrus / earthy / pine / floral / spicy) nudges ranking with REAL batch-lab terpenes.

A product whose strongest three lab terpenes carry the aroma gets engine.AROMA_BOOST added to its score and,
if it is shown, one factual line in its reason ("Citrus-forward — limonene leads"). No slot, an unknown
slot value, or no lab data -> ranking, reasons and queries are exactly what they were. Offline, DB only.
"""
import json
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from budtender import compliance, engine, lab_enrich, ranking, terpenes, views
from budtender.models import BatchLab, Product
from budtender.ranking import rank_products
from budtender.tests.test_lab_picks import CACHES_LOCMEM, TOKEN, _flower, _lab

# A margin 30 -> owner slot #1; B velocity 9 -> slot #2; C..F differ only by a hair of margin, so their
# order is margin order until a boost arrives. F: limonene leads · D: limonene is third · E: myrcene leads.
BASE = ["A", "B", "C", "D", "E", "F"]


def _stock():
    _flower("A", price=30, margin=30, batch="BA")
    _flower("B", price=30, margin=20, velocity=9, batch="BB")
    _flower("C", price=30, margin=12.0, batch="BC")
    _flower("D", price=30, margin=11.9, batch="BD")
    _flower("E", price=30, margin=11.8, batch="BE")
    _flower("F", price=30, margin=11.7, batch="BF")
    _lab("BD", terpenes=(("Beta-Caryophyllene", 1.5), ("Beta-Myrcene", 1.0), ("Limonene", 0.5)))
    _lab("BE", terpenes=(("Beta-Myrcene", 1.4), ("Alpha-Pinene", 0.2)))
    _lab("BF", terpenes=(("Limonene", 0.9), ("Beta-Caryophyllene", 0.4)))


def _rank(slots=None, **kw):
    ranked = rank_products("yakima", {"category": "flower", **(slots or {})}, None, limit=kw.pop("limit", 6), **kw)
    return [(p.sku, why) for p, why in ranked]


class AromaRankingTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _stock()
        self.base = _rank()

    def test_the_baseline_is_the_owner_ordering_then_margin(self):
        self.assertEqual([s for s, _ in self.base], BASE)

    def test_citrus_lifts_the_limonene_batches_past_equal_picks_but_not_the_owner_slots(self):
        got = _rank({"aroma": "citrus"})
        self.assertEqual([s for s, _ in got], ["A", "B", "D", "F", "C", "E"])  # D and F up; slots 1-2 unmoved

    def test_earthy_lifts_both_myrcene_batches_the_lead_and_the_runner_up(self):
        got = _rank({"aroma": "earthy"})
        self.assertEqual([s for s, _ in got], ["A", "B", "D", "E", "C", "F"])   # D has myrcene second, E first
        self.assertEqual(dict(got)["E"], "Earthy-forward — myrcene leads")
        self.assertEqual(dict(got)["D"], "Earthy notes — myrcene is a top terpene")

    def test_the_reorder_is_the_boost_and_nothing_else(self):
        # zero the boost: the citrus ask must stop reordering (it is the engine's one constant, imported here)
        self.assertIs(ranking.AROMA_BOOST, engine.AROMA_BOOST)
        with mock.patch.object(ranking, "AROMA_BOOST", 0.0):
            self.assertEqual([s for s, _ in _rank({"aroma": "citrus"})], BASE)

    def test_no_slot_unknown_or_junk_values_change_nothing_at_all(self):
        for junk in (None, "", "banana", "Citrus", "citrus ", ["citrus"], {"citrus": 1}, 7, True):
            slots = {"aroma": junk}
            self.assertEqual(_rank(slots), self.base, junk)          # same picks, same order, same reasons

    def test_a_valid_aroma_with_no_lab_data_changes_nothing(self):
        BatchLab.objects.all().delete()
        cache.clear()
        baseline = _rank()
        for aroma in terpenes.AROMA_TERPENES:
            self.assertEqual(_rank({"aroma": aroma}), baseline, aroma)

    def test_a_valid_aroma_nothing_in_the_labs_carries_changes_nothing(self):
        # none of the three labs has linalool or terpinolene in its top three
        self.assertEqual(_rank({"aroma": "floral"}), self.base)

    def test_a_none_status_or_empty_lab_row_is_no_data_not_a_match(self):
        BatchLab.objects.filter(batch_id="BF").update(status="none", data={})
        self.assertEqual([s for s, _ in _rank({"aroma": "citrus"})], ["A", "B", "D", "C", "E", "F"])

    def test_a_terpene_outside_the_top_three_does_not_count(self):
        _lab("BC", terpenes=(("Beta-Myrcene", 1.0), ("Beta-Caryophyllene", 0.9), ("Alpha-Pinene", 0.8),
                             ("Limonene", 0.7)))   # limonene is FOURTH
        self.assertEqual([s for s, _ in _rank({"aroma": "citrus"})], ["A", "B", "D", "F", "C", "E"])

    def test_it_never_outranks_the_stock_price_and_exclusion_guards(self):
        _flower("LOW", price=30, margin=50, quantity_on_hand=2, batch="BL")        # under the floor stock
        _flower("PRICEY", price=500, margin=50, batch="BP")                        # outside the budget
        _flower("SHOWN", price=30, margin=50, batch="BS")                          # already shown
        for batch in ("BL", "BP", "BS"):
            _lab(batch, terpenes=(("Limonene", 5.0),))
        got = [s for s, _ in _rank({"aroma": "citrus", "price_max": 100}, exclude_skus={"SHOWN"})]
        for gone in ("LOW", "PRICEY", "SHOWN"):
            self.assertNotIn(gone, got)

    def test_a_clear_margin_lead_still_beats_the_aroma_nudge(self):
        BatchLab.objects.all().delete()
        Product.objects.exclude(sku__in=["A", "C"]).delete()
        _flower("THIN", price=30, margin=2, batch="BT")       # citrus-led but a far thinner margin than C
        _lab("BT", terpenes=(("Limonene", 2.0),))
        cache.clear()
        # A margin 30 is #1; C (margin 12) must stay above THIN (margin 2) even with the boost
        self.assertEqual([s for s, _ in _rank({"aroma": "citrus"})], ["A", "C", "THIN"])

    def test_the_reason_says_so_only_where_the_lab_really_matched(self):
        whys = dict(_rank({"aroma": "citrus"}))
        self.assertEqual(whys["F"], "Citrus-forward — limonene leads")
        self.assertEqual(whys["D"], "Citrus notes — limonene is a top terpene")
        for sku in ("A", "B", "C", "E"):     # no lab / no citrus terpene: nothing about aroma
            self.assertNotIn("itrus", whys[sku], sku)
            self.assertNotIn("forward", whys[sku], sku)
        for text in whys.values():
            self.assertEqual(compliance.therapeutic_hits(text), [], text)
            self.assertEqual(compliance.injection_hits(text), [], text)

    def test_no_aroma_slot_never_mentions_an_aroma(self):
        for _, why in self.base:
            self.assertNotIn("forward", why)
            self.assertNotIn("notes", why)

    def test_the_lab_read_stays_one_bulk_query_and_costs_nothing_without_the_slot(self):
        def reads(slots):
            """(batchlab SQL statements, batch-id counts handed to labs_for) for a 2-pick request."""
            seen, real = [], lab_enrich.labs_for

            def spy(ids, memo=None):
                seen.append(len([i for i in ids if i]))
                return real(ids, memo=memo)

            with mock.patch.object(lab_enrich, "labs_for", spy), CaptureQueriesContext(connection) as ctx:
                _rank(slots, limit=2)
            return [q for q in ctx.captured_queries if "budtender_batchlab" in q["sql"]], seen

        (base_q, base_ids), (junk_q, junk_ids), (citrus_q, citrus_ids) = (
            reads({}), reads({"aroma": "banana"}), reads({"aroma": "citrus"}))
        self.assertEqual((len(base_q), base_ids), (1, [2]))        # the 2 picks' reasons read their labs once
        self.assertEqual((len(junk_q), junk_ids), (1, [2]))        # an unknown value changes nothing
        # a real aroma reads ALL 6 candidates, in ONE query; the picks' own read is then a memo hit
        self.assertEqual((len(citrus_q), citrus_ids), (1, [6, 2]))


@override_settings(CACHES=CACHES_LOCMEM, HHT_BACKEND_TOKEN=TOKEN)
class AromaThroughTheEndpointTests(TestCase):
    """The slot rides `slots` through the real HTTP view to the ranker, and the card's lab profile carries the
    plain-words explanation built from the same stored lab."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch.object(views, "inventory_is_stale", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        _stock()

    def _search(self, slots):
        resp = Client().post(
            "/api/v1/products/search/",
            data=json.dumps({"slots": {"store": "yakima", "category": "flower", **slots}, "limit": 6}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["results"]

    def test_the_aroma_slot_reaches_the_ranker_and_the_reason_reaches_the_card(self):
        results = self._search({"aroma": "citrus"})
        self.assertEqual([r["sku"] for r in results], ["A", "B", "D", "F", "C", "E"])
        by_sku = {r["sku"]: r for r in results}
        self.assertEqual(by_sku["F"]["why_this"], "Citrus-forward — limonene leads")
        self.assertEqual([r["sku"] for r in self._search({})], BASE)

    def test_the_card_profile_has_the_explanation_and_aroma_for_a_lab_and_empty_ones_without(self):
        by_sku = {r["sku"]: r for r in self._search({})}
        profile = by_sku["F"]["lab"]["profile"]
        self.assertEqual(profile["aroma"], ["citrus", "spicy"])
        self.assertEqual(profile["explain"], "Smells citrusy and spicy (limonene, caryophyllene). Customers often "
                                             "describe profiles like this as uplifting — everyone is different.")
        self.assertIsNone(by_sku["C"]["lab"])
