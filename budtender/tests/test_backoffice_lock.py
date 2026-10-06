"""ONE Dutchie-backoffice lock for everything that spends the 60-calls/minute budget (New Drops, the lab and
product-detail warm, the on-demand warm), with an OWNER TOKEN.

Before: New Drops took "newdrops:lock", the warm took "batchlab:lock", and New Drops never looked at the
warm's, so the two could run at once (pacing is per process) and double the rate. And `finally: delete(key)`
removed whoever held the key by then: a run that outlived the TTL deleted the NEXT run's lock.
"""
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from budtender import backoffice_lock, new_drops, tasks


class LockTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_one_holder_at_a_time_and_the_token_is_the_proof_of_ownership(self):
        a = backoffice_lock.acquire(60)
        self.assertTrue(a)
        self.assertIsNone(backoffice_lock.acquire(60))
        self.assertTrue(backoffice_lock.is_held())
        self.assertEqual(cache.get(backoffice_lock.KEY), a)         # the value IS the token
        backoffice_lock.release(a)
        self.assertFalse(backoffice_lock.is_held())
        self.assertTrue(backoffice_lock.acquire(60))                # free again

    def test_a_run_that_outlived_its_ttl_does_not_delete_the_next_runs_lock(self):
        a = backoffice_lock.acquire(60)
        cache.delete(backoffice_lock.KEY)                           # A's TTL ran out while it was still working
        b = backoffice_lock.acquire(60)                             # B legitimately takes over
        self.assertTrue(b)
        self.assertNotEqual(a, b)
        backoffice_lock.release(a)                                  # A finally finishes: must NOT free B's lock
        self.assertEqual(cache.get(backoffice_lock.KEY), b)
        backoffice_lock.release(b)
        self.assertIsNone(cache.get(backoffice_lock.KEY))

    def test_releasing_with_nothing_or_a_stale_token_is_harmless(self):
        backoffice_lock.release(None)
        backoffice_lock.release("nope")
        c = backoffice_lock.acquire(60)
        backoffice_lock.release("nope")
        self.assertEqual(cache.get(backoffice_lock.KEY), c)


class SharedByEveryDutchieJobTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_new_drops_holds_the_shared_lock_while_it_runs_and_the_warm_cannot_start(self):
        seen = {}

        def during_refresh(slug, **kw):
            seen["held"] = cache.get(backoffice_lock.KEY)
            with mock.patch("budtender.lab_enrich.warm") as w:
                seen["warm_result"] = tasks.warm_batch_labs_all(force=True)
                seen["warm_ran"] = w.called
            return {"brands": []}

        with mock.patch.object(new_drops, "refresh_store", side_effect=during_refresh), \
             mock.patch.object(new_drops, "backfill_lab", return_value=0):
            tasks.refresh_new_drops_all(force=True)
        self.assertTrue(seen["held"])
        self.assertEqual(seen["warm_result"], {"skipped": "backoffice_busy"})
        self.assertFalse(seen["warm_ran"])
        self.assertIsNone(cache.get(backoffice_lock.KEY))           # released at the end

    def test_the_warm_holds_the_shared_lock_while_it_runs_and_new_drops_cannot_start(self):
        seen = {}

        def during_warm(slug, **kw):
            seen["held"] = cache.get(backoffice_lock.KEY)
            with mock.patch.object(new_drops, "refresh_store") as r:
                seen["drops_result"] = tasks.refresh_new_drops_all(force=True)
                seen["drops_ran"] = r.called
            return {"ok": 0}

        with mock.patch("budtender.lab_enrich.warm", side_effect=during_warm):
            tasks.warm_batch_labs_all(force=True)
        self.assertTrue(seen["held"])
        self.assertEqual(seen["drops_result"], {"skipped": "previous_run_still_going"})
        self.assertFalse(seen["drops_ran"])

    def test_a_new_drops_run_that_outlives_its_lock_does_not_free_the_next_holders(self):
        stolen = {}

        def slow_refresh(slug, **kw):
            cache.set(backoffice_lock.KEY, "someone-elses-token", 60)   # our TTL expired, another job took over
            stolen["token"] = "someone-elses-token"
            return {"brands": []}

        with mock.patch.object(new_drops, "refresh_store", side_effect=slow_refresh), \
             mock.patch.object(new_drops, "backfill_lab", return_value=0):
            tasks.refresh_new_drops_all(force=True)
        self.assertEqual(cache.get(backoffice_lock.KEY), stolen["token"])

    def test_the_warm_that_outlives_its_lock_does_not_free_the_next_holders(self):
        def slow_warm(slug, **kw):
            cache.set(backoffice_lock.KEY, "someone-elses-token", 60)
            return {"ok": 0}

        with mock.patch("budtender.lab_enrich.warm", side_effect=slow_warm):
            tasks.warm_batch_labs_all(force=True)
        self.assertEqual(cache.get(backoffice_lock.KEY), "someone-elses-token")

    def test_both_commands_use_the_shared_lock(self):
        held = backoffice_lock.acquire(60)
        with self.assertRaises(CommandError):
            call_command("warm_batch_labs", "--store", "yakima", stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command("refresh_new_drops", "--store", "yakima", stdout=StringIO())
        self.assertEqual(cache.get(backoffice_lock.KEY), held)      # a refused command never frees someone else's

    def test_a_command_that_ran_frees_only_its_own_lock(self):
        with mock.patch("budtender.lab_enrich.warm", return_value={"ok": 0, "none": 0, "failed": 0,
                                                                   "stopped": False, "unresolved": []}):
            call_command("warm_batch_labs", "--store", "yakima", "--pause", "0", stdout=StringIO())
        self.assertIsNone(cache.get(backoffice_lock.KEY))
        with mock.patch.object(new_drops, "refresh_store", return_value={"brands": []}):
            call_command("refresh_new_drops", "--store", "yakima", stdout=StringIO())
        self.assertIsNone(cache.get(backoffice_lock.KEY))
