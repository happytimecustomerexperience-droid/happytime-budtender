"""The owner's ranking-weights override lives in the Setting table, not only in the cache."""
from __future__ import annotations

import json
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from budtender import views
from budtender.models import AdminAudit, Setting

BACKEND, WEBSITE = "backend-token", "website-token"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PUSH = {"w_anon": {"margin": 2}, "w_known": {"affinity": 3, "margin": 1}, "margin_emphasis": 1.75, "actor": "owner"}


def _post(path, payload, token=BACKEND):
    return Client().post(path, data=json.dumps(payload), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class RankingWeightsPersistTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def _search_weights(self, token=WEBSITE, body=None):
        """The ranking_weights the search handed to the ranker."""
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products", return_value=[]) as ranker:
            _post("/api/v1/products/search/", {"slots": {"store": "yakima"}, **(body or {})}, token)
        return ranker.call_args.kwargs["ranking_weights"]

    def test_the_push_is_stored_in_the_setting_table_and_audited_with_key_names_only(self):
        r = _post("/api/v1/admin/ranking-weights", PUSH)
        self.assertEqual(r.status_code, 200)
        applied = r.json()["applied"]
        self.assertEqual(Setting.objects.get(key="ranking_weights").value, applied)
        self.assertEqual(applied["margin_emphasis"], 1.75)
        audit = AdminAudit.objects.get(action="ranking_weights.set")
        self.assertEqual((audit.actor, audit.target), ("owner", "ranking_weights"))
        self.assertEqual(audit.after, {"keys": ["margin_emphasis", "w_anon", "w_known"]})
        self.assertEqual(audit.before, {"had_override": False})
        self.assertNotIn("1.75", json.dumps([audit.before, audit.after]))   # keys, never values

    def test_a_second_push_replaces_the_one_row_and_records_that_there_was_one(self):
        _post("/api/v1/admin/ranking-weights", PUSH)
        _post("/api/v1/admin/ranking-weights", {**PUSH, "margin_emphasis": 0.5})
        self.assertEqual(Setting.objects.filter(key="ranking_weights").count(), 1)
        self.assertEqual(Setting.objects.get(key="ranking_weights").value["margin_emphasis"], 0.5)
        last = AdminAudit.objects.filter(action="ranking_weights.set").order_by("-id").first()
        self.assertEqual(last.before, {"had_override": True})

    def test_validation_is_unchanged_and_the_stored_value_is_the_cleaned_one(self):
        _post("/api/v1/admin/ranking-weights", {"w_anon": {"margin": 2, "bad": 999}, "margin_emphasis": "bad"})
        stored = Setting.objects.get(key="ranking_weights").value
        self.assertEqual(stored["margin_emphasis"], 1.0)
        self.assertNotIn("bad", stored["w_anon"])

    def test_the_override_survives_a_restart_or_eviction_of_the_cache(self):
        applied = _post("/api/v1/admin/ranking-weights", PUSH).json()["applied"]
        cache.clear()                                    # Redis flushed / process restarted
        self.assertEqual(self._search_weights(), applied)
        self.assertEqual(cache.get(views._RANKING_WEIGHTS_CACHE_KEY), applied)   # read-through re-warmed it

    def test_a_later_push_is_seen_by_the_next_search_not_the_stale_cache(self):
        _post("/api/v1/admin/ranking-weights", PUSH)
        newer = _post("/api/v1/admin/ranking-weights", {**PUSH, "margin_emphasis": 3}).json()["applied"]
        self.assertEqual(self._search_weights()["margin_emphasis"], newer["margin_emphasis"])

    def test_no_override_means_the_ranker_gets_none_and_nothing_is_cached(self):
        self.assertIsNone(self._search_weights())
        self.assertIsNone(cache.get(views._RANKING_WEIGHTS_CACHE_KEY))

    def test_the_voices_per_request_weights_still_win_and_the_website_cannot_set_them(self):
        _post("/api/v1/admin/ranking-weights", PUSH)
        mine = {"w_anon": {"margin": 9}}
        self.assertEqual(self._search_weights(BACKEND, {"ranking_weights": mine}), mine)
        self.assertNotEqual(self._search_weights(WEBSITE, {"ranking_weights": mine}), mine)
