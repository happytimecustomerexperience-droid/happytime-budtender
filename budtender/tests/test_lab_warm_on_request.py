"""A pick shown to a customer with no stored lab/detail fills in for the NEXT viewer.

The request path is DB-only: it never calls Dutchie. It enqueues ONE deduped task for the ids that are
missing or stale; the task warms just those ids under the same lock and pacing the beat uses. A broker that
is down must never fail or slow a search. (conftest replaces `tasks.warm_ids` with a recording mock, so no
test here can reach a real broker.)
"""
import json
from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from budtender import backoffice_lock, lab_enrich, new_drops, tasks, views
from budtender.models import BatchLab, Product, ProductDetail
from budtender.tasks import warm_ids as REAL_WARM_IDS  # captured before conftest swaps the module attribute
from budtender.tests.test_lab_enrich import LAB_FLOWER, NO_LAB
from budtender.tests.test_product_detail import EMPTY_RECORD, GOOD, _routed_client

TOKEN = "test-token"
CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
NOW = timezone.now()


def _flower(sku, batch="", pid="", **kw):
    defaults = dict(location_slug="yakima", name=f"Flower {sku}", brand=f"Brand {sku}", category="flower",
                    price=30, cost=10, margin=20, quantity_on_hand=10, availability=True, batch_id=batch,
                    product_id=pid, thc_percent=22.0)
    defaults.update(kw)
    return Product.objects.create(sku=sku, **defaults)


def _enqueued():
    return tasks.warm_ids.apply_async.call_args_list


@override_settings(CACHES=CACHES_LOCMEM, HHT_BACKEND_TOKEN=TOKEN)
class EnqueueFromTheRequestPathTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.client = Client()
        patcher = mock.patch.object(views, "inventory_is_stale", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _search(self, **slots):
        resp = self.client.post(
            "/api/v1/products/search/",
            data=json.dumps({"slots": {"store": "yakima", "category": "flower", **slots}, "limit": 5}),
            content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["results"]

    def test_missing_labs_and_details_enqueue_exactly_one_task_with_the_missing_ids(self):
        _flower("A", "301", "501")
        _flower("B", "302", "502")
        self._search()
        self.assertEqual(len(_enqueued()), 1)
        call = _enqueued()[0]
        self.assertEqual(call.kwargs.get("retry"), False)  # a down broker must not stall the search
        loc, batches, pids = call.kwargs["args"][:3]
        self.assertEqual((loc, sorted(batches), sorted(pids)), ("yakima", ["301", "302"], ["501", "502"]))

    def test_the_same_ids_are_not_enqueued_twice_within_the_dedupe_window(self):
        _flower("A", "301", "501")
        self._search()
        self._search()
        self._search()
        self.assertEqual(len(_enqueued()), 1)

    def test_a_new_pick_enqueues_only_what_is_new(self):
        _flower("A", "301", "501")
        self._search()
        _flower("B", "302", "502", price=31)
        self._search()
        self.assertEqual(len(_enqueued()), 2)
        _loc, batches, pids = _enqueued()[1].kwargs["args"][:3]
        self.assertEqual((batches, pids), (["302"], ["502"]))

    def test_stored_fresh_data_is_never_enqueued(self):
        _flower("OK", "301", "501")
        lab_enrich.record("301", LAB_FLOWER, "flower")
        lab_enrich.record_detail("501", GOOD)
        _flower("NONE", "302", "502", price=31)
        lab_enrich.record("302", NO_LAB, "flower")              # fresh 'none': Dutchie said so < 7 days ago
        lab_enrich.record_detail("502", EMPTY_RECORD)
        self._search()
        self.assertEqual(_enqueued(), [])

    def test_a_week_old_none_and_a_week_old_detail_are_enqueued_again(self):
        _flower("A", "301", "501")
        lab_enrich.record("301", NO_LAB, "flower")
        BatchLab.objects.filter(batch_id="301").update(checked_at=NOW - timedelta(days=8))
        lab_enrich.record_detail("501", GOOD)
        ProductDetail.objects.filter(product_id="501").update(checked_at=NOW - timedelta(days=8))
        self._search()
        _loc, batches, pids = _enqueued()[0].kwargs["args"][:3]
        self.assertEqual((batches, pids), (["301"], ["501"]))

    def test_a_pick_with_no_batch_or_product_id_enqueues_nothing_for_it(self):
        _flower("A", "", "")
        self._search()
        self.assertEqual(_enqueued(), [])

    def test_only_the_picks_are_considered_not_every_candidate(self):
        for i in range(12):
            _flower(f"S{i}", f"4{i:02d}", f"6{i:02d}", price=30 + i)
        self._search()
        _loc, batches, pids = _enqueued()[0].kwargs["args"][:3]
        self.assertEqual((len(batches), len(pids)), (5, 5))   # limit=5 picks, not the 12 in stock

    def test_the_request_path_never_calls_dutchie(self):
        _flower("A", "301", "501")
        # RECORD the calls instead of raising: a tripwire that raises into code wrapped in `except Exception`
        # (enqueue_missing's is) would be swallowed and the test would stay green with Dutchie being called.
        calls = []

        def tripwire(name):
            return lambda *a, **k: calls.append(name)

        with mock.patch.object(new_drops, "lab_for_batch", side_effect=tripwire("lab_for_batch")), \
             mock.patch.object(new_drops, "_client", side_effect=tripwire("_client")), \
             mock.patch.object(new_drops.BackofficeClient, "post", side_effect=tripwire("post")), \
             mock.patch.object(new_drops.BackofficeClient, "get_product_details",
                               side_effect=tripwire("get_product_details")), \
             mock.patch.object(new_drops.BackofficeClient, "session_block", side_effect=tripwire("session_block")), \
             mock.patch("dutchie.session.http_post", side_effect=tripwire("http_post")), \
             mock.patch("dutchie.session.login_employee", side_effect=tripwire("login_employee")):
            results = self._search()
            by_sku = self.client.get("/api/v1/products/by-sku/", {"store": "yakima", "sku": "A"},
                                     HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual([r["sku"] for r in results], ["A"])
        self.assertEqual(by_sku.status_code, 200)
        self.assertEqual(len(_enqueued()), 1)  # the enqueue happened...
        self.assertEqual(calls, [])            # ...and nothing reached Dutchie, swallowed or not

    def test_the_tripwire_style_can_actually_see_a_call(self):
        """Control: the recording tripwire above is not hollow, so prove it fires on a real call."""
        calls = []
        with mock.patch.object(new_drops.BackofficeClient, "post", side_effect=lambda *a, **k: calls.append("post")):
            try:
                new_drops.BackofficeClient.post(object.__new__(new_drops.BackofficeClient), "/x", {})
            except Exception:  # noqa: BLE001 - swallowed on purpose, exactly like the code under test
                pass
        self.assertEqual(calls, ["post"])

    def test_a_failed_enqueue_does_not_suppress_the_retry_for_30_minutes(self):
        _flower("A", "301", "501")
        tasks.warm_ids.apply_async.side_effect = ConnectionError("broker down")
        with self.assertLogs("budtender.lab_enrich", level="WARNING"):
            self._search()
        self.assertIsNone(cache.get("labwarm:b:301"))                   # no dedupe key for something never queued
        self.assertIsNone(cache.get("labwarm:p:501"))
        tasks.warm_ids.apply_async.side_effect = None
        cache.delete("labwarm:down")                                    # the 60 s breaker has passed
        self._search()
        self.assertEqual(len(_enqueued()), 2)                           # the same ids went out on the retry

    def test_a_successful_enqueue_sets_the_dedupe_keys_after_the_call(self):
        _flower("A", "301", "501")
        self._search()
        self.assertTrue(cache.get("labwarm:b:301"))
        self.assertTrue(cache.get("labwarm:p:501"))

    def test_nothing_is_enqueued_when_celery_runs_eagerly(self):
        # eager = the "worker" is this request thread: a warm would make Dutchie calls inside the search
        _flower("A", "301", "501")
        with override_settings(CELERY_TASK_ALWAYS_EAGER=True):
            results = self._search()
        self.assertEqual([r["sku"] for r in results], ["A"])
        self.assertEqual(_enqueued(), [])
        self.assertIsNone(cache.get("labwarm:b:301"))

    def test_a_broker_that_is_down_never_fails_the_search(self):
        _flower("A", "301", "501")
        tasks.warm_ids.apply_async.side_effect = ConnectionError("broker down")
        with self.assertLogs("budtender.lab_enrich", level="WARNING"):
            results = self._search()
        self.assertEqual([r["sku"] for r in results], ["A"])

    def test_after_a_broker_failure_the_next_searches_do_not_try_again_for_a_minute(self):
        _flower("A", "301", "501")
        tasks.warm_ids.apply_async.side_effect = ConnectionError("broker down")
        with self.assertLogs("budtender.lab_enrich", level="WARNING"):
            self._search()
        _flower("B", "302", "502", price=31)
        self._search()
        self.assertEqual(len(_enqueued()), 1)  # the breaker spared the second request the connect timeout

    def test_a_cache_that_is_down_never_fails_the_search(self):
        _flower("A", "301", "501")
        down = mock.Mock()
        down.get.return_value = None
        down.add.side_effect = ConnectionError("cache down")
        with mock.patch("budtender.lab_enrich.cache", down), self.assertLogs("budtender.lab_enrich", level="WARNING"):
            results = self._search()
        self.assertEqual([r["sku"] for r in results], ["A"])

    def test_by_sku_and_pairing_enqueue_too(self):
        _flower("A", "301", "501")
        r = self.client.get("/api/v1/products/by-sku/", {"store": "yakima", "sku": "A"},
                            HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(_enqueued()), 1)
        pair = _flower("PAIR", "302", "502", category="edibles")
        with mock.patch.object(views, "pair_for", return_value=(pair, "complement", "goes well", 0.5)):
            r = self.client.post("/api/v1/pairing/for-sku", data=json.dumps({"location": "yakima", "sku": "A"}),
                                 content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {TOKEN}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(_enqueued()), 2)

    def test_the_search_attaches_what_is_stored_with_one_bulk_read_per_table(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for i in range(5):
            _flower(f"S{i}", f"4{i:02d}", f"6{i:02d}", price=30 + i)
            lab_enrich.record(f"4{i:02d}", LAB_FLOWER, "flower")
            lab_enrich.record_detail(f"6{i:02d}", GOOD)
        with CaptureQueriesContext(connection) as ctx:
            results = self._search()
        self.assertTrue(all(r["lab"] and r["info"] for r in results))
        for table in ("budtender_batchlab", "budtender_productdetail"):
            hits = [q["sql"] for q in ctx.captured_queries if table in q["sql"]]
            self.assertEqual(len(hits), 1, (table, hits))


class WarmIdsTaskTests(TestCase):
    """The task itself (the real one: conftest swapped the module attribute, REAL_WARM_IDS is the original)."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _flower("P0", "300", "500", velocity=9)
        _flower("P1", "301", "501", velocity=8)

    def _run(self, *args, **kw):
        client = _routed_client(labs={"300": LAB_FLOWER, "301": LAB_FLOWER}, details={"500": GOOD, "501": GOOD})
        with mock.patch.object(new_drops, "_client", return_value=client), \
             mock.patch("budtender.lab_enrich.time.sleep"):
            out = REAL_WARM_IDS(*args, **kw)
        return out, client

    def test_it_warms_just_the_given_ids(self):
        out, client = self._run("yakima", ["300"], ["500"])
        self.assertEqual(client.calls, ["/api/v2/batches/300/lab-results", "details:500"])
        self.assertEqual(list(BatchLab.objects.values_list("batch_id", flat=True)), ["300"])
        self.assertEqual(list(ProductDetail.objects.values_list("product_id", flat=True)), ["500"])
        self.assertIsNone(cache.get(backoffice_lock.KEY))          # the shared lock is released

    def test_it_takes_the_beats_lock_so_two_runs_never_double_the_call_rate(self):
        seen = {}

        def during(*a, **k):
            seen["held"] = cache.get(backoffice_lock.KEY)
            return {"ok": 0, "none": 0, "failed": 0, "stopped": False, "unresolved": []}

        with mock.patch("budtender.lab_enrich.warm", side_effect=during):
            REAL_WARM_IDS("yakima", ["300"], ["500"])
        self.assertTrue(seen["held"])

    def test_while_the_beats_lock_is_held_it_retries_later_with_a_countdown(self):
        cache.set(backoffice_lock.KEY, 1, 60)
        with mock.patch("budtender.lab_enrich.warm") as w:
            out = REAL_WARM_IDS("yakima", ["300"], ["500"])
        w.assert_not_called()
        self.assertEqual(out, {"deferred": 1})
        call = tasks.warm_ids.apply_async.call_args
        self.assertEqual(call.kwargs["args"], ["yakima", ["300"], ["500"], 1])
        self.assertGreater(call.kwargs["countdown"], 0)
        self.assertEqual(call.kwargs.get("retry"), False)
        self.assertTrue(cache.get(backoffice_lock.KEY))            # someone else's lock is left alone

    def test_a_new_drops_run_defers_it_too(self):
        cache.set(backoffice_lock.KEY, 1, 60)
        with mock.patch("budtender.lab_enrich.warm") as w:
            self.assertEqual(REAL_WARM_IDS("yakima", ["300"], ["500"]), {"deferred": 1})
        w.assert_not_called()

    def test_after_a_few_deferrals_it_gives_up_and_the_beat_covers_it(self):
        cache.set(backoffice_lock.KEY, 1, 60)
        with mock.patch("budtender.lab_enrich.warm") as w, self.assertLogs("budtender.tasks", level="INFO"):
            out = REAL_WARM_IDS("yakima", ["300"], ["500"], 3)
        w.assert_not_called()
        self.assertEqual(out, {"gave_up": True})
        tasks.warm_ids.apply_async.assert_not_called()

    def test_a_failure_inside_the_run_still_frees_the_lock(self):
        with mock.patch("budtender.lab_enrich.warm", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                REAL_WARM_IDS("yakima", ["300"], ["500"])
        self.assertIsNone(cache.get(backoffice_lock.KEY))

    def test_an_unknown_store_is_refused_not_queried(self):
        with mock.patch("budtender.lab_enrich.warm") as w:
            self.assertEqual(REAL_WARM_IDS("nowhere", ["300"], ["500"]), {"skipped": "unknown_store"})
        w.assert_not_called()
