"""BatchLab: the durable per-batch lab table, the paced warmer, and the inventory-sync fill.

No network: Dutchie is a mock client (or a mocked ``lab_for_batch``), never real. The point
of the table is the difference between EMPTY (Dutchie answered: no lab data -> 'none', re-checked
after 7 days) and UNREACHABLE (no answer -> no row at all, so a blip never reads as "no lab").
"""
import json
import unittest
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from budtender import backoffice_lock, lab_enrich, new_drops, tasks
from budtender.models import BatchLab, Product
from budtender.tests.test_new_drops import LAB_BEVERAGE, LAB_FLOWER

NOW = timezone.now()

# What Dutchie really answers for a batch with no lab: the FULL shape, flagged HasLabData:false, every
# value null (structure copied from the real capture below). An EMPTY Data is something else: an anomaly.
_NULL = {"Value": None, "UnitId": None}
NO_LAB = {"BatchId": 301, "HasLabData": False,
          "TestDetails": {"LabName": None, "TestedDate": None, "CoaUrl": None},
          "Cannabinoids": {"Thc": dict(_NULL), "Thca": dict(_NULL), "Cbd": dict(_NULL), "Cbg": dict(_NULL)},
          "TotalCannabinoids": dict(_NULL),
          "Terpenes": {"Limonene": dict(_NULL), "BetaMyrcene": dict(_NULL)},
          "TotalTerpenes": dict(_NULL),
          "Contaminants": {"Pesticides": None, "HeavyMetal": None, "Mycotoxin": None, "Microbiology": None,
                           "SolventResidue": None}}


def _product(sku, batch, **kw):
    defaults = dict(location_slug="yakima", name=f"Flower {sku}", category="flower", price=30, cost=10,
                    margin=20, quantity_on_hand=10, availability=True, batch_id=batch)
    defaults.update(kw)
    return Product.objects.create(sku=sku, **defaults)


def _client(responses):
    """A BackofficeClient stand-in: post() pops canned responses, an Exception is raised."""
    c = mock.Mock()
    c.session_block.return_value = {}
    calls = []

    def post(path, body, **kw):
        calls.append(path)
        r = responses[len(calls) - 1] if isinstance(responses, list) else responses
        if isinstance(r, Exception):
            raise r
        return {"Data": r}

    c.post.side_effect = post
    c.calls = calls
    return c


class LabFromDataTests(SimpleTestCase):
    def test_contract_a_shape_without_profile(self):
        self.assertEqual(lab_enrich.lab_from_data(LAB_FLOWER, "flower"), {
            "total_terpenes": 3.17,
            "terpenes": [{"name": "Beta-Caryophyllene", "pct": 1.56}, {"name": "Beta-Myrcene", "pct": 1.26},
                         {"name": "Alpha-Pinene", "pct": 0.24}, {"name": "Limonene", "pct": 0.11}],
            "cbd_total": None, "thc_total": 26.0, "minor_cannabinoids": [], "tested_date": None,
            "lab_name": None, "coa_url": "https://certs.conflabs.com/x.pdf", "contaminants": {}})

    def test_terpenes_are_top_five_only(self):
        many = {"Terpenes": {f"T{i}": {"Value": 0.1 * (i + 1), "UnitId": 2} for i in range(8)}}
        out = lab_enrich.lab_from_data(many, "flower")
        self.assertEqual(len(out["terpenes"]), 5)
        self.assertEqual(out["terpenes"][0]["pct"], 0.8)
        self.assertEqual(out["total_terpenes"], round(0.1 * sum(range(1, 9)), 2))

    def test_a_trace_terpene_that_rounds_to_zero_is_not_listed(self):
        out = lab_enrich.lab_from_data({"Terpenes": {"Limonene": {"Value": 0.004, "UnitId": 2},
                                                      "Linalool": {"Value": 0.3, "UnitId": 2}}}, "flower")
        self.assertEqual([t["name"] for t in out["terpenes"]], ["Linalool"])

    def test_empty_means_nothing_at_all_on_file(self):
        self.assertTrue(lab_enrich.is_empty(lab_enrich.lab_from_data({}, "flower")))
        self.assertFalse(lab_enrich.is_empty(lab_enrich.lab_from_data(LAB_FLOWER, "flower")))
        # A COA link alone IS a lab on file (that is what the card links to).
        self.assertFalse(lab_enrich.is_empty(lab_enrich.lab_from_data(LAB_BEVERAGE, "beverages")))


CAPTURE = Path(__file__).parent / "data" / "backoffice_capture_2026-10-05" / "batch_lab_results.json"


@unittest.skipUnless(CAPTURE.exists(), f"real backoffice capture not found: {CAPTURE}")
class RealCaptureTests(SimpleTestCase):
    """A real /api/v2/batches/{id}/lab-results response (captured 2026-10-05): the shape every other
    fixture here was written to imitate. Skips (never substitutes synthetic data) when absent."""

    def setUp(self):
        self.data = json.loads(CAPTURE.read_text(encoding="utf-8"))["Data"]

    def test_the_labs_own_total_wins_over_the_sum_of_the_listed_terpenes(self):
        listed = sum(v["Value"] for v in self.data["Terpenes"].values() if v["Value"])
        self.assertAlmostEqual(listed, 7.7, places=1)  # the lab rounds its own total to 10
        self.assertEqual(lab_enrich.lab_from_data(self.data, "concentrates")["total_terpenes"], 10)

    def test_contract_a_from_a_real_lab(self):
        lab = lab_enrich.lab_from_data(self.data, "concentrates")
        self.assertEqual([(t["name"], t["pct"]) for t in lab["terpenes"]],
                         [("Terpinolene", 2.7), ("Beta-Myrcene", 2.0), ("Beta-Caryophyllene", 0.93),
                          ("Limonene", 0.65), ("Humulene", 0.32)])
        self.assertEqual((lab["thc_total"], lab["cbd_total"]), (74.5, None))   # 66 + 0.877*9.7; CBD < 1
        self.assertEqual(lab["tested_date"], "2026-08-27")
        self.assertIsNone(lab["lab_name"])
        self.assertTrue(lab["coa_url"].startswith("https://"))
        self.assertEqual(lab_enrich.with_profile(lab)["profile"], {
            "lean": None, "notes": ["floral", "earthy", "pepper"],
            "line": "Terpinolene-led (2.7%) — floral.",   # the source text claims no effect for it
            "explain": "Smells floral, earthy and spicy (terpinolene, myrcene, caryophyllene).",
            "aroma": ["floral", "earthy", "spicy"]})

    def test_zero_and_null_terpenes_are_not_listed_and_the_total_is_not_a_terpene(self):
        names = " ".join(t["name"] for t in lab_enrich.lab_from_data(self.data, "concentrates")["terpenes"])
        for absent in ("Camphene", "Eucalyptol", "Fenchol", "Total"):
            self.assertNotIn(absent, names)

    def test_the_whole_contract_a_lab_pinned_against_the_real_payload(self):
        self.assertEqual(lab_enrich.lab_from_data(self.data, "concentrates"), {
            "total_terpenes": 10,
            "terpenes": [{"name": "Terpinolene", "pct": 2.7}, {"name": "Beta-Myrcene", "pct": 2.0},
                         {"name": "Beta-Caryophyllene", "pct": 0.93}, {"name": "Limonene", "pct": 0.65},
                         {"name": "Humulene", "pct": 0.32}],
            "cbd_total": None, "thc_total": 74.5,
            "minor_cannabinoids": [{"name": "CBG", "pct": 1.5}, {"name": "CBC", "pct": 0.54},
                                   {"name": "THCV", "pct": 0.47}],
            "tested_date": "2026-08-27", "lab_name": None,
            "coa_url": self.data["TestDetails"]["CoaUrl"],
            "contaminants": {"pesticides": "pass", "heavy_metals": "pass", "mycotoxin": "pass",
                             "solvents": "pass"}})
        self.assertTrue(self.data["TestDetails"]["CoaUrl"].startswith("https://storage.googleapis.com/"))

    def test_a_contaminant_is_listed_only_when_dutchie_says_pass(self):
        self.assertIsNone(self.data["Contaminants"]["Microbiology"])  # null in the real lab: never implied
        self.assertNotIn("microbiology", lab_enrich.lab_from_data(self.data, "concentrates")["contaminants"])


class ContaminantsAndMinorsTests(SimpleTestCase):
    def _lab(self, **data):
        return lab_enrich.lab_from_data(data, "flower")

    def test_pass_is_case_insensitive_and_a_null_screen_is_simply_left_out(self):
        out = self._lab(Contaminants={"Pesticides": "PASS", "HeavyMetal": " Pass ", "Mycotoxin": None,
                                      "Microbiology": None, "SolventResidue": ""})
        self.assertEqual(out["contaminants"], {"pesticides": "pass", "heavy_metals": "pass"})

    def test_one_screen_that_is_neither_null_nor_pass_omits_the_whole_dict(self):
        # never show partial passes next to a fail (or next to anything we cannot read as a pass)
        for odd in ("fail", "FAIL", "not tested", "pending", True, 1, ["pass"]):
            out = self._lab(Contaminants={"Pesticides": "pass", "HeavyMetal": "pass", "Mycotoxin": odd,
                                          "Microbiology": None, "SolventResidue": "pass"})
            self.assertEqual(out["contaminants"], {}, odd)

    def test_no_contaminant_data_is_an_empty_dict_not_a_pass(self):
        self.assertEqual(self._lab(Contaminants=None)["contaminants"], {})
        self.assertEqual(self._lab(Contaminants={"Pesticides": True, "HeavyMetal": 1})["contaminants"], {})

    def test_minor_cannabinoids_are_the_top_three_uppercased_without_the_big_four(self):
        cann = {"Thc": {"Value": 20, "UnitId": 2}, "Thca": {"Value": 5, "UnitId": 2},
                "Cbd": {"Value": 4, "UnitId": 2}, "Cbda": {"Value": 3, "UnitId": 2},
                "Cbg": {"Value": 0.9, "UnitId": 2}, "Cbn": {"Value": 0.2, "UnitId": 2},
                "Thcv": {"Value": 0.5, "UnitId": 2}, "Cbc": {"Value": 0.4, "UnitId": 2},
                "Cbl": {"Value": 0, "UnitId": 2}, "Cbt": {"Value": None, "UnitId": None},
                "Cbdv": {"Value": 7, "UnitId": 3}, "Cannabinoid": None}
        self.assertEqual(self._lab(Cannabinoids=cann)["minor_cannabinoids"],
                         [{"name": "CBG", "pct": 0.9}, {"name": "THCV", "pct": 0.5}, {"name": "CBC", "pct": 0.4}])

    def test_a_minor_cannabinoid_in_another_unit_or_with_no_unit_is_not_listed(self):
        cann = {"Cbg": {"Value": 1.0, "UnitId": 7}, "Cbn": {"Value": 0.4, "UnitId": None},
                "Cbc": {"Value": 0.3, "UnitId": 2}}
        self.assertEqual(self._lab(Cannabinoids=cann)["minor_cannabinoids"], [{"name": "CBC", "pct": 0.3}])

    def test_edible_weight_percent_potency_hides_the_minors_too(self):
        lab = lab_enrich.lab_from_data({"Cannabinoids": {"Cbg": {"Value": 0.5, "UnitId": 2}}}, "edibles")
        self.assertEqual(lab["minor_cannabinoids"], [])

    def test_a_lab_with_only_contaminants_or_minors_is_still_a_lab_on_file(self):
        self.assertFalse(lab_enrich.is_empty(self._lab(Contaminants={"Pesticides": "pass"})))
        self.assertFalse(lab_enrich.is_empty(self._lab(Cannabinoids={"Cbg": {"Value": 0.5, "UnitId": 2}})))


class LabForBatchAnomalyTests(SimpleTestCase):
    """new_drops.lab_for_batch: an empty/missing Data is an anomaly, never an authoritative "no lab"."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _ask(self, resp):
        client = mock.Mock()
        client.session_block.return_value = {}
        client.post.return_value = resp
        return new_drops.lab_for_batch(client, 77), client

    def test_missing_empty_or_non_dict_data_returns_none_and_is_not_cached(self):
        for resp in ({}, {"Data": None}, {"Data": {}}, {"Data": []}, {"Data": "x"}, {"Result": True}):
            got, _ = self._ask(resp)
            self.assertIsNone(got, resp)
            self.assertIsNone(cache.get("newdrops:lab:77"), resp)

    def test_the_real_no_lab_shape_is_an_answer_and_is_cached(self):
        got, _ = self._ask({"Data": NO_LAB})
        self.assertEqual(got, NO_LAB)
        self.assertEqual(cache.get("newdrops:lab:77"), NO_LAB)

    def test_a_lab_is_returned_and_cached(self):
        got, _ = self._ask({"Data": LAB_FLOWER})
        self.assertEqual(got, LAB_FLOWER)
        self.assertEqual(cache.get("newdrops:lab:77"), LAB_FLOWER)

    def test_the_read_is_marked_idempotent_so_throttles_and_blips_are_retried(self):
        _, client = self._ask({"Data": LAB_FLOWER})
        self.assertIs(client.post.call_args.kwargs.get("idempotent"), True)


class BackofficeClientPassThroughTests(SimpleTestCase):
    """The backoffice client's own pacing wraps PosClient.post and must hand `idempotent` through."""

    def setUp(self):
        self.client = object.__new__(new_drops.BackofficeClient)
        new_drops.BackofficeClient._last_call = 0.0

    def test_idempotent_reaches_the_base_client(self):
        with mock.patch.object(new_drops.PosClient, "post", return_value={"Result": True}) as p, \
             mock.patch.object(new_drops.time, "sleep"):
            self.client.post("/x", {}, idempotent=True)
        self.assertIs(p.call_args.kwargs["idempotent"], True)

    def test_the_default_stays_non_idempotent(self):
        with mock.patch.object(new_drops.PosClient, "post", return_value={"Result": True}) as p, \
             mock.patch.object(new_drops.time, "sleep"):
            self.client.post("/x", {})
        self.assertFalse(p.call_args.kwargs.get("idempotent", False))

    def test_the_61_second_rate_limit_retry_keeps_the_flag(self):
        err = RuntimeError("Result=false: 'Too many requests - only 60 per minute allowed'")
        with mock.patch.object(new_drops.PosClient, "post", side_effect=[err, {"Result": True}]) as p, \
             mock.patch.object(new_drops.time, "sleep"):
            self.client.post("/x", {}, idempotent=True)
        self.assertEqual([c.kwargs["idempotent"] for c in p.call_args_list], [True, True])


class HelperTests(SimpleTestCase):
    def test_effective_thc_prefers_inventory_and_falls_back_to_the_lab(self):
        self.assertEqual(lab_enrich.effective_thc(20.0, {"thc_total": 26.0}), 20.0)
        self.assertEqual(lab_enrich.effective_thc(None, {"thc_total": 26.0}), 26.0)
        self.assertEqual(lab_enrich.effective_thc(0.0, {"thc_total": 26.0}), 0.0)
        self.assertIsNone(lab_enrich.effective_thc(None, {"thc_total": None}))
        self.assertIsNone(lab_enrich.effective_thc(None, None))

    def test_dominant_terpene_is_the_canonical_top_one_or_blank(self):
        self.assertEqual(lab_enrich.dominant_terpene({"terpenes": [{"name": "Beta-Myrcene", "pct": 1.2}]}), "myrcene")
        self.assertEqual(lab_enrich.dominant_terpene({"terpenes": []}), "")
        self.assertEqual(lab_enrich.dominant_terpene(None), "")

    def test_with_profile_adds_the_words_and_leaves_the_numbers(self):
        lab = lab_enrich.lab_from_data(LAB_FLOWER, "flower")
        out = lab_enrich.with_profile(lab)
        self.assertEqual({k: v for k, v in out.items() if k != "profile"}, lab)
        self.assertEqual(out["profile"]["line"], "Caryophyllene-led (1.56%) — pepper.")
        self.assertNotIn("profile", lab)  # the stored dict is never mutated


class RecordTests(TestCase):
    def test_a_lab_is_stored_ok_without_a_profile(self):
        self.assertEqual(lab_enrich.record("101", LAB_FLOWER, "flower"), "ok")
        row = BatchLab.objects.get(batch_id="101")
        self.assertEqual(row.status, "ok")
        self.assertNotIn("profile", row.data)
        self.assertEqual(row.data["thc_total"], 26.0)
        self.assertIsNotNone(row.checked_at)

    def test_dutchie_answered_with_nothing_is_none(self):
        self.assertEqual(lab_enrich.record("102", NO_LAB, "flower"), "none")
        self.assertEqual(BatchLab.objects.get(batch_id="102").status, "none")
        self.assertEqual(BatchLab.objects.get(batch_id="102").data, {})

    def test_an_empty_or_unstructured_answer_is_an_anomaly_that_stores_nothing(self):
        # Only the real no-lab SHAPE (or HasLabData:false) is an authoritative "none".
        for odd in ({}, {"Message": "ok"}, {"BatchId": 5}, {"TestDetails": {"CoaUrl": None}}, [], "x"):
            self.assertIsNone(lab_enrich.record("107", odd, "flower"), odd)
            self.assertFalse(BatchLab.objects.filter(batch_id="107").exists(), odd)

    def test_has_lab_data_false_alone_is_an_authoritative_none(self):
        self.assertEqual(lab_enrich.record("108", {"HasLabData": False}, "flower"), "none")

    def test_the_expected_structure_with_nothing_in_it_is_a_none(self):
        self.assertEqual(lab_enrich.record("109", {"Terpenes": {}}, "flower"), "none")
        self.assertEqual(lab_enrich.record("110", {"Cannabinoids": {"Thc": {"Value": None, "UnitId": None}}},
                                           "flower"), "none")

    def test_has_lab_data_true_with_content_is_ok(self):
        self.assertEqual(lab_enrich.record("111", {**LAB_FLOWER, "HasLabData": True}, "flower"), "ok")

    def test_unreachable_writes_no_row_at_all(self):
        self.assertIsNone(lab_enrich.record("103", None, "flower"))
        self.assertFalse(BatchLab.objects.filter(batch_id="103").exists())

    def test_unreachable_never_turns_an_existing_none_into_anything(self):
        lab_enrich.record("104", NO_LAB, "flower")
        stamp = BatchLab.objects.get(batch_id="104").checked_at
        self.assertIsNone(lab_enrich.record("104", None, "flower"))
        row = BatchLab.objects.get(batch_id="104")
        self.assertEqual((row.status, row.checked_at), ("none", stamp))

    def test_a_none_becomes_ok_when_the_lab_finally_shows_up(self):
        lab_enrich.record("105", NO_LAB, "flower")
        lab_enrich.record("105", LAB_FLOWER, "flower")
        self.assertEqual(BatchLab.objects.filter(batch_id="105").count(), 1)
        self.assertEqual(BatchLab.objects.get(batch_id="105").status, "ok")

    def test_batch_id_is_unique(self):
        from django.db import IntegrityError, transaction
        BatchLab.objects.create(batch_id="1", status="ok", data={}, checked_at=NOW)
        with self.assertRaises(IntegrityError), transaction.atomic():
            BatchLab.objects.create(batch_id="1", status="none", data={}, checked_at=NOW)


class LabsForTests(TestCase):
    def test_one_bulk_query_and_only_ok_rows_are_returned(self):
        lab_enrich.record("1", LAB_FLOWER, "flower")
        lab_enrich.record("2", NO_LAB, "flower")
        with self.assertNumQueries(1):
            got = lab_enrich.labs_for(["1", "2", "3", "", "1"])
        self.assertEqual(set(got), {"1"})
        self.assertEqual(got["1"]["thc_total"], 26.0)

    def test_a_none_row_is_never_a_lab_even_if_it_somehow_carries_data(self):
        BatchLab.objects.create(batch_id="9", status="none", data={"thc_total": 9.0, "terpenes": []},
                                checked_at=NOW)
        self.assertEqual(lab_enrich.labs_for(["9"]), {})

    def test_the_memo_answers_hits_and_misses_without_asking_again(self):
        lab_enrich.record("1", LAB_FLOWER, "flower")
        memo = {}
        lab_enrich.labs_for(["1", "2"], memo=memo)
        with self.assertNumQueries(0):
            got = lab_enrich.labs_for(["1", "2"], memo=memo)
        self.assertEqual(set(got), {"1"})

    def test_no_ids_no_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(lab_enrich.labs_for(["", None]), {})


class WarmSelectionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _product("A", "201", velocity=1)                                  # needs a lab
        _product("B", "202", velocity=9)                                  # needs a lab, sells fastest
        _product("OUT", "203", availability=False)                        # not on the floor
        _product("NOB", "")                                               # no batch
        _product("JUNK", "not-a-number")                                  # never builds a URL from this
        _product("HAVE", "205")
        lab_enrich.record("205", LAB_FLOWER, "flower")                    # already ok
        _product("FRESH", "206")
        lab_enrich.record("206", NO_LAB, "flower")                            # none, checked just now
        _product("STALE", "207")
        lab_enrich.record("207", NO_LAB, "flower")
        BatchLab.objects.filter(batch_id="207").update(checked_at=NOW - timedelta(days=8))  # due again
        _product("OTHER", "208", location_slug="pullman")                 # another store

    def test_selects_in_stock_batches_with_no_ok_lab_fastest_sellers_first(self):
        todo = lab_enrich.select_todo("yakima", limit=50, now=NOW)
        self.assertEqual([b for b, _c in todo], ["202", "201", "207"])

    def test_none_rows_are_rechecked_only_after_seven_days(self):
        BatchLab.objects.filter(batch_id="206").update(checked_at=NOW - timedelta(days=6, hours=23))
        self.assertNotIn("206", [b for b, _ in lab_enrich.select_todo("yakima", limit=50, now=NOW)])
        BatchLab.objects.filter(batch_id="206").update(checked_at=NOW - timedelta(days=7, minutes=1))
        self.assertIn("206", [b for b, _ in lab_enrich.select_todo("yakima", limit=50, now=NOW)])

    def test_an_ok_row_is_asked_again_only_after_30_days(self):
        self.assertEqual(lab_enrich.RECHECK_OK_AFTER, timedelta(days=30))
        BatchLab.objects.filter(batch_id="205").update(checked_at=NOW - timedelta(days=29, hours=23))
        self.assertNotIn("205", [b for b, _ in lab_enrich.select_todo("yakima", limit=50, now=NOW)])
        BatchLab.objects.filter(batch_id="205").update(checked_at=NOW - timedelta(days=30, minutes=1))
        self.assertIn("205", [b for b, _ in lab_enrich.select_todo("yakima", limit=50, now=NOW)])

    def test_limit_caps_the_run(self):
        self.assertEqual([b for b, _ in lab_enrich.select_todo("yakima", limit=1, now=NOW)], ["202"])

    def test_dry_run_builds_no_client_and_writes_nothing(self):
        with mock.patch.object(new_drops, "_client") as make, \
             mock.patch.object(new_drops, "lab_for_batch") as fetch:
            out = lab_enrich.warm("yakima", limit=50, dry_run=True, now=NOW)
        make.assert_not_called()
        fetch.assert_not_called()
        self.assertEqual(out["would_fetch"], ["202", "201", "207"])
        self.assertEqual(BatchLab.objects.count(), 3)


@mock.patch("budtender.lab_enrich.time.sleep")
class WarmRunTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _warm(self, client, **kw):
        with mock.patch.object(new_drops, "_client", return_value=client):
            return lab_enrich.warm("yakima", now=NOW, **kw)

    def test_a_good_run_writes_ok_and_none_rows_and_pauses_between_network_calls(self, sleep):
        for i in range(3):
            _product(f"P{i}", f"30{i}", velocity=10 - i)
        client = _client([LAB_FLOWER, NO_LAB, LAB_FLOWER])
        out = self._warm(client, pause=0.7)
        self.assertEqual((out["ok"], out["none"], out["failed"], out["stopped"]), (2, 1, 0, False))
        self.assertEqual(dict(BatchLab.objects.values_list("batch_id", "status")),
                         {"300": "ok", "301": "none", "302": "ok"})
        self.assertEqual(client.calls, [f"/api/v2/batches/30{i}/lab-results" for i in range(3)])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.7, 0.7])  # between 3 calls, not before the 1st

    def test_a_cached_lab_costs_no_call_and_no_pause(self, sleep):
        _product("P0", "300")
        cache.set("newdrops:lab:300", LAB_FLOWER, 60)
        client = _client([])
        out = self._warm(client, pause=0.7)
        self.assertEqual(client.calls, [])
        sleep.assert_not_called()
        self.assertEqual(out["ok"], 1)
        self.assertEqual(BatchLab.objects.get(batch_id="300").status, "ok")

    def test_an_unreachable_dutchie_writes_no_row(self, sleep):
        _product("P0", "300")
        out = self._warm(_client([RuntimeError("503 upstream")]), pause=0)
        self.assertEqual((out["ok"], out["none"], out["failed"]), (0, 0, 1))
        self.assertEqual(out["unresolved"], ["300"])
        self.assertFalse(BatchLab.objects.filter(batch_id="300").exists())

    def test_a_due_none_is_really_rechecked_not_answered_from_the_30_day_cache(self, sleep):
        _product("P0", "300")
        lab_enrich.record("300", NO_LAB, "flower")
        BatchLab.objects.filter(batch_id="300").update(checked_at=NOW - timedelta(days=9))
        cache.set("newdrops:lab:300", {}, 60)  # the cached "empty" that would otherwise answer
        client = _client([LAB_FLOWER])
        out = self._warm(client, pause=0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(out["ok"], 1)
        self.assertEqual(BatchLab.objects.get(batch_id="300").status, "ok")

    def test_an_ok_lab_is_rechecked_after_30_days_and_corrected_from_a_fresh_fetch(self, sleep):
        # a stored lab used to be immutable forever: a lab corrected at the source could never reach a customer
        _product("P0", "300")
        lab_enrich.record("300", LAB_FLOWER, "flower")                              # thc 26.0 on file
        BatchLab.objects.filter(batch_id="300").update(checked_at=NOW - timedelta(days=31))
        cache.set("newdrops:lab:300", LAB_FLOWER, 60)       # the cached RAW copy would answer if it were not dropped
        corrected = {**LAB_FLOWER, "Cannabinoids": {"Thc": {"Value": 0.4, "UnitId": 2}, "Thca": {"Value": 22.0, "UnitId": 2}}}
        client = _client([corrected])
        out = self._warm(client, pause=0)
        self.assertEqual(len(client.calls), 1)                                      # really re-fetched
        self.assertEqual(out["ok"], 1)
        self.assertEqual(BatchLab.objects.get(batch_id="300").data["thc_total"], round(0.4 + 0.877 * 22.0, 1))

    def test_an_ok_lab_under_30_days_old_is_left_alone(self, sleep):
        _product("P0", "300")
        lab_enrich.record("300", LAB_FLOWER, "flower")
        BatchLab.objects.filter(batch_id="300").update(checked_at=NOW - timedelta(days=29))
        client = _client([])
        self._warm(client, pause=0)
        self.assertEqual(client.calls, [])

    def test_a_recheck_that_comes_back_empty_or_no_lab_never_downgrades_a_good_lab(self, sleep):
        for answer in (NO_LAB, {}):
            BatchLab.objects.all().delete()
            Product.objects.all().delete()
            _product("P0", "300")
            lab_enrich.record("300", LAB_FLOWER, "flower")
            old = NOW - timedelta(days=40)
            BatchLab.objects.filter(batch_id="300").update(checked_at=old)
            self._warm(_client([answer]), pause=0)
            row = BatchLab.objects.get(batch_id="300")
            self.assertEqual(row.status, "ok", answer)                                # the lab on file survives
            self.assertEqual(row.data["thc_total"], 26.0, answer)

    def test_a_no_lab_answer_on_a_recheck_refreshes_the_check_time_so_it_is_not_asked_every_run(self, sleep):
        _product("P0", "300")
        lab_enrich.record("300", LAB_FLOWER, "flower")
        BatchLab.objects.filter(batch_id="300").update(checked_at=NOW - timedelta(days=40))
        self._warm(_client([NO_LAB]), pause=0)
        self.assertEqual([b for b, _ in lab_enrich.select_todo("yakima", limit=10, now=NOW)], [])

    def test_a_cached_no_lab_answer_in_dutchies_real_shape_is_not_trusted_either(self, sleep):
        # What Dutchie returns for a batch with no lab is NOT an empty dict: it is the full shape, all null.
        no_lab = {"BatchId": 301, "HasLabData": False, "TestDetails": {"CoaUrl": None, "LabName": None},
                  "Cannabinoids": {"Thc": {"Value": None, "UnitId": None}},
                  "Terpenes": {"Limonene": {"Value": None, "UnitId": None}},
                  "TotalTerpenes": {"Value": None, "UnitId": None}}
        _product("P0", "301")
        cache.set("newdrops:lab:301", no_lab, 60)
        client = _client([LAB_FLOWER])
        out = self._warm(client, pause=0)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(out["ok"], 1)

    def test_that_no_lab_shape_is_recorded_as_none(self, sleep):
        no_lab = {"HasLabData": False, "TestDetails": {"CoaUrl": None},
                  "Cannabinoids": {"Thc": {"Value": None, "UnitId": None}},
                  "Terpenes": {"Limonene": {"Value": None, "UnitId": None}},
                  "TotalTerpenes": {"Value": None, "UnitId": None}}
        _product("P0", "302")
        out = self._warm(_client([no_lab]), pause=0)
        self.assertEqual((out["ok"], out["none"]), (0, 1))
        self.assertEqual(BatchLab.objects.get(batch_id="302").status, "none")

    def test_stops_after_three_identical_failures_and_names_the_unresolved_ids(self, sleep):
        for i in range(6):
            _product(f"P{i}", f"30{i}", velocity=10 - i)
        client = _client(RuntimeError("503 upstream"))
        with self.assertLogs("budtender.lab_enrich", level="ERROR") as logs:
            out = self._warm(client, pause=0)
        self.assertEqual(len(client.calls), 3)
        self.assertTrue(out["stopped"])
        self.assertEqual(out["failed"], 3)
        self.assertEqual(out["unresolved"], [f"30{i}" for i in range(6)])
        joined = " ".join(logs.output)
        for i in range(6):
            self.assertIn(f"30{i}", joined)
        self.assertEqual(BatchLab.objects.count(), 0)

    def test_a_success_in_between_resets_the_streak(self, sleep):
        for i in range(6):
            _product(f"P{i}", f"30{i}", velocity=10 - i)
        boom = RuntimeError("503 upstream")
        client = _client([boom, boom, LAB_FLOWER, boom, boom, LAB_FLOWER])
        out = self._warm(client, pause=0)
        self.assertFalse(out["stopped"])
        self.assertEqual((out["ok"], out["failed"]), (2, 4))
        self.assertEqual(len(client.calls), 6)

    def test_no_credentials_is_one_loud_failure_not_a_crash(self, sleep):
        _product("P0", "300")
        with mock.patch.object(new_drops, "_client", side_effect=RuntimeError("DUTCHIE_BACKOFFICE_USERS is empty")), \
             self.assertLogs("budtender.lab_enrich", level="ERROR"):
            out = lab_enrich.warm("yakima", now=NOW)
        self.assertEqual(out["unresolved"], ["300"])
        self.assertTrue(out["stopped"])

    def test_the_category_reaches_the_potency_rule(self, sleep):
        # THC >= 1 on purpose: below 1 the "t >= 1" rule hides it whatever the category, so a test on the
        # real LAB_BEVERAGE (THC 0.001) could not tell a working category rule from a missing one.
        edible_lab = {**LAB_BEVERAGE, "Cannabinoids": {"Thc": {"Value": 12.0, "UnitId": 2},
                                                       "Cbd": {"Value": 3.0, "UnitId": 2}}}
        _product("P0", "300", category="edibles", velocity=9)
        _product("P1", "301", category="flower", velocity=8)
        self._warm(_client([edible_lab, edible_lab]), pause=0)
        self.assertIsNone(BatchLab.objects.get(batch_id="300").data["thc_total"])      # edible: never a weight %
        self.assertEqual(BatchLab.objects.get(batch_id="301").data["thc_total"], 12.0)  # the control: flower is


class WarmCommandAndTaskTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _product("A", "201")

    def test_command_dry_run_lists_ids_and_touches_nothing(self):
        out = StringIO()
        with mock.patch.object(new_drops, "_client") as make:
            call_command("warm_batch_labs", "--store", "yakima", "--limit", "5", "--dry-run", stdout=out)
        make.assert_not_called()
        self.assertIn("201", out.getvalue())
        self.assertEqual(BatchLab.objects.count(), 0)

    def test_command_runs_the_warmer_per_store_with_its_options(self):
        with mock.patch("budtender.lab_enrich.warm", return_value={"ok": 1, "none": 0, "failed": 0,
                                                                   "stopped": False, "unresolved": []}) as w:
            call_command("warm_batch_labs", "--store", "yakima", "--limit", "7", "--pause", "0", stdout=StringIO())
        self.assertEqual(w.call_args.args, ("yakima",))
        self.assertEqual((w.call_args.kwargs["limit"], w.call_args.kwargs["pause"]), (7, 0.0))

    def test_command_exits_non_zero_when_the_run_had_to_stop(self):
        with mock.patch("budtender.lab_enrich.warm", return_value={"ok": 0, "none": 0, "failed": 3,
                                                                   "stopped": True, "unresolved": ["201"]}):
            with self.assertRaises(CommandError):
                call_command("warm_batch_labs", "--store", "yakima", stdout=StringIO())

    def test_command_refuses_to_overlap_a_new_drops_run(self):
        cache.set(backoffice_lock.KEY, 1, 60)
        with self.assertRaises(CommandError):
            call_command("warm_batch_labs", "--store", "yakima", stdout=StringIO())

    def test_task_is_a_noop_when_every_store_is_closed(self):
        with mock.patch("budtender.tasks.any_store_open_or_warming", return_value=False), \
             mock.patch("budtender.lab_enrich.warm") as w:
            self.assertEqual(tasks.warm_batch_labs_all(), {"skipped": "stores_closed"})
        w.assert_not_called()

    def test_task_yields_to_a_running_new_drops_refresh(self):
        cache.set(backoffice_lock.KEY, 1, 60)
        with mock.patch("budtender.lab_enrich.warm") as w:
            self.assertEqual(tasks.warm_batch_labs_all(force=True), {"skipped": "backoffice_busy"})
        w.assert_not_called()

    def test_task_one_store_failing_does_not_block_the_others_and_frees_its_lock(self):
        def fake(slug, **kw):
            if slug == "yakima":
                raise RuntimeError("boom")
            return {"ok": 1}

        with mock.patch("budtender.lab_enrich.warm", side_effect=fake):
            out = tasks.warm_batch_labs_all(force=True)
        self.assertEqual(out["yakima"], "error: RuntimeError")
        self.assertEqual(out["pullman"], {"ok": 1})
        self.assertIsNone(cache.get(backoffice_lock.KEY))

    def test_task_overlapping_run_is_skipped(self):
        cache.set(backoffice_lock.KEY, 1, 60)
        with mock.patch("budtender.lab_enrich.warm") as w:
            self.assertEqual(tasks.warm_batch_labs_all(force=True), {"skipped": "backoffice_busy"})
        w.assert_not_called()

    def test_beat_schedule_points_at_the_task(self):
        from core.celery import app
        tasks_scheduled = {e["task"] for e in app.conf.beat_schedule.values()}
        self.assertIn("budtender.tasks.warm_batch_labs_all", tasks_scheduled)


class SyncFillsDominantTerpeneTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _row(self, sku, batch, **kw):
        return {"sku": sku, "name": f"Flower {sku}", "category": "flower", "price": 30, "cost": 10,
                "quantity_on_hand": 9, "batch_id": batch, "dominant_terpene": "", **kw}

    def _sync(self, rows):
        with mock.patch("budtender.tasks.dutchie.fetch_inventory", return_value=rows):
            return tasks.sync_inventory("yakima")

    def test_the_synced_product_gets_the_canonical_top_terpene_from_its_batch_lab(self):
        lab_enrich.record("401", LAB_FLOWER, "flower")  # caryophyllene leads LAB_FLOWER
        self._sync([self._row("A", "401")])
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "caryophyllene")

    def test_no_lab_row_means_blank_not_a_guess(self):
        lab_enrich.record("402", NO_LAB, "flower")  # a 'none' row has no terpene to lend
        self._sync([self._row("A", "402"), self._row("B", "403"), self._row("C", "")])
        self.assertEqual(set(Product.objects.values_list("dominant_terpene", flat=True)), {""})

    def test_a_terpene_the_inventory_row_already_carries_is_kept(self):
        lab_enrich.record("401", LAB_FLOWER, "flower")
        self._sync([self._row("A", "401", dominant_terpene="limonene")])
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "limonene")

    def test_a_stored_terpene_is_never_blanked_when_the_sync_has_nothing_for_it(self):
        # the sync only FILLS dominant_terpene: a lab row that is missing (or a failed/empty lab read) must not
        # erase what an earlier sync stored, or the terpene signal flickers on and off with every blip
        lab_enrich.record("401", LAB_FLOWER, "flower")
        self._sync([self._row("A", "401")])
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "caryophyllene")
        BatchLab.objects.all().delete()                                  # the lab row is gone / not found this time
        self._sync([self._row("A", "401")])
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "caryophyllene")
        with mock.patch("budtender.tasks.lab_enrich.labs_for", return_value={}):   # the lab read returned nothing
            self._sync([self._row("A", "401", dominant_terpene="")])
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "caryophyllene")

    def test_a_new_non_empty_terpene_still_replaces_the_stored_one(self):
        lab_enrich.record("401", LAB_FLOWER, "flower")
        self._sync([self._row("A", "401")])
        self._sync([self._row("A", "401", dominant_terpene="limonene")])   # the inventory feed says so
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "limonene")
        lab_enrich.record("402", {**LAB_FLOWER, "Terpenes": {"Linalool": {"Value": 2.0, "UnitId": 2}}}, "flower")
        self._sync([self._row("A", "402")])                                # a new batch whose lab says linalool
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "linalool")

    def test_the_next_sync_fills_a_product_once_its_lab_lands(self):
        self._sync([self._row("A", "401")])
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "")
        lab_enrich.record("401", LAB_FLOWER, "flower")
        self._sync([self._row("A", "401")])
        self.assertEqual(Product.objects.get(sku="A").dominant_terpene, "caryophyllene")

    def test_one_bulk_lab_query_per_store_sync_not_one_per_product(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for i in range(6):
            lab_enrich.record(f"50{i}", LAB_FLOWER, "flower")
        with CaptureQueriesContext(connection) as ctx:
            self._sync([self._row(f"S{i}", f"50{i}") for i in range(6)])
        lab_queries = [q for q in ctx.captured_queries if "budtender_batchlab" in q["sql"]]
        self.assertEqual(len(lab_queries), 1, [q["sql"] for q in lab_queries])
