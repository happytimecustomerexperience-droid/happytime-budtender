"""Chat analytics: the event whitelist, the per-session funnel, the session timeline, and the
append-only conversation store (conversations are never deleted or shortened)."""
from __future__ import annotations

import json
from datetime import timedelta

from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from budtender import analytics
from budtender.models import AnalyticsEvent, ChatMessage, ChatSession, SuggestedProduct

BACKEND, WEBSITE = "backend-token", "website-token"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
NOW = timezone.now()


def _post(path, payload, token=BACKEND):
    return Client().post(path, data=json.dumps(payload), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


def _ev(session, name, secs, store="pullman", visitor="v1", **props):
    """One stored event at NOW + secs (ts is auto_now_add, so it is moved after the insert)."""
    e = AnalyticsEvent.objects.create(session_token=session, visitor_id=visitor, event_type=name,
                                      location_slug=store, props=props)
    AnalyticsEvent.objects.filter(pk=e.pk).update(ts=NOW - timedelta(minutes=30) + timedelta(seconds=secs))
    return e


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class TrackWhitelistTests(TestCase):
    def test_every_contract_event_is_stored_and_unknown_names_are_dropped(self):
        names = sorted(analytics.CONTRACT_EVENTS)
        events = [{"event": n, "visitor_id": "vis-1", "session_id": "s-abc", "path": "/menu", "device_type": "mobile",
                   "ts": 1, "props": {"store": "mount-vernon", "step": "category"}} for n in names]
        events += [{"event": "drop_table", "session_id": "s-abc"}, {"event": "x" * 40}, {"event": ""}, "junk"]
        body = _post("/api/v1/track/", {"v": 1, "events": events}, WEBSITE).json()
        self.assertEqual((body["stored"], body["ignored"]), (len(names), 3))
        row = AnalyticsEvent.objects.get(event_type="search_run")
        self.assertEqual((row.session_token, row.visitor_id, row.location_slug), ("s-abc", "vis-1", "mount-vernon"))
        self.assertFalse(AnalyticsEvent.objects.filter(event_type="drop_table").exists())

    def test_the_names_the_live_site_sends_today_are_still_stored(self):
        for n in ("chat_open", "chat_search", "chat_product_click", "page_view", "web_vital", "phone_click",
                  "dutchie_product_view", "chat_session_end"):
            self.assertIn(n, analytics.EVENT_WHITELIST, n)
        for n in analytics.EVENT_WHITELIST:
            self.assertLessEqual(len(n), 32, n)

    def test_pii_props_are_dropped_and_the_phone_is_hashed(self):
        _post("/api/v1/track/", {"event_type": "phone_capture_submit", "session_token": "s-x", "phone": "(509) 555-0142",
                                 "props": {"phone": "5095550142", "email": "a@b.c", "step": "phone"}}, WEBSITE)
        row = AnalyticsEvent.objects.get()
        self.assertEqual(row.props, {"step": "phone"})
        self.assertEqual(len(row.phone_hash), 64)
        self.assertNotIn("5095550142", row.phone_hash)

    def test_a_batch_is_capped(self):
        events = [{"event": "chip_click", "session_id": "s-1"} for _ in range(150)]
        body = _post("/api/v1/track/", {"events": events}, WEBSITE).json()
        self.assertEqual(body["stored"], 100)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class FunnelTests(TestCase):
    URL = "/api/v1/analytics/funnel"

    def setUp(self):
        # A: pullman, full path to order-ahead.
        for i, (n, kw) in enumerate([
            ("chat_open", {}), ("questionnaire_step_view", {"step": "category"}),
            ("questionnaire_step_answer", {"step": "category", "value": "flower"}),
            ("questionnaire_step_view", {"step": "budget"}), ("questionnaire_step_answer", {"step": "budget"}),
            ("search_run", {"slots_summary": "store=pullman · category=flower · effect=sleep · budget=$30-50", "count": 5}),
            ("picks_view", {}), ("show_more_click", {"offset": 5}), ("product_card_click", {}),
            ("order_ahead_click", {"sku": "S1"}), ("find_similar_open", {})]):
            _ev("s-A", n, i * 10, store="pullman", visitor="v-A", **kw)
        # B: mount-vernon, opened and left (bounce, no interaction).
        _ev("s-B", "chat_open", 5, store="mount-vernon", visitor="v-B")
        _ev("s-B", "nav_away", 9, store="mount-vernon", visitor="v-B", had_open_chat=True)
        # C: mount-vernon, saw picks then left; stalled at the budget step; also a zero-result search first.
        for i, (n, kw) in enumerate([
            ("chat_open", {}), ("questionnaire_step_view", {"step": "category"}),
            ("questionnaire_step_answer", {"step": "category"}), ("questionnaire_step_view", {"step": "budget"}),
            ("search_run", {"slots_summary": "category=edible · size=100mg", "count": 0}),
            ("search_run", {"slots_summary": "category=vape · budget=$20", "count": 3}), ("picks_view", {})]):
            _ev("s-C", n, 20 + i * 10, store="mount-vernon", visitor="v-C", **kw)
        # D: legacy names only (what the live site sends today), yakima, clicked a product.
        _ev("s-D", "chat_open", 40, store="yakima", visitor="v-D")
        _ev("s-D", "chat_search", 45, store="yakima", visitor="v-D", category="flower")
        _ev("s-D", "chat_recommend_view", 50, store="yakima", visitor="v-D")
        _ev("s-D", "chat_product_click", 55, store="yakima", visitor="v-D", sku="S9")
        # page-only noise and an old event outside the window never count.
        _ev("t-page", "page_view", 1)
        old = _ev("s-old", "chat_open", 0)
        AnalyticsEvent.objects.filter(pk=old.pk).update(ts=NOW - timedelta(days=90))

    def test_funnel_numbers(self):
        r = _post(self.URL, {"days": 30}).json()
        self.assertEqual((r["sessions"], r["unique_visitors"], r["opens"]), (4, 4, 4))
        stages = {s["stage"]: s["sessions"] for s in r["funnel"]}
        self.assertEqual(stages, {"opened_chat": 4, "interacted": 3, "searched": 3, "saw_picks": 3,
                                  "clicked_a_product": 2, "order_ahead": 1})
        a = r["actions"]
        self.assertEqual((a["searches"], a["zero_result_searches"], a["show_more_clicks"], a["product_card_clicks"],
                          a["order_ahead_clicks"], a["find_similar_opens"]), (4, 1, 1, 2, 1, 1))
        self.assertEqual(r["bounces"]["no_interaction"], 1)
        self.assertEqual(r["bounces"]["left_after_picks"], 1)
        self.assertEqual(r["bounces"]["total"], 2)
        self.assertEqual(r["bounces"]["rate"], 0.5)
        self.assertEqual(r["outcomes"], {"order_ahead": 1, "bounce_no_interaction": 1, "left_after_picks": 1,
                                         "clicked_product": 1})

    def test_questionnaire_dropoff_by_step_in_the_order_asked(self):
        steps = _post(self.URL, {}).json()["questionnaire_steps"]
        self.assertEqual([s["step"] for s in steps], ["category", "budget"])
        self.assertEqual((steps[0]["viewed"], steps[0]["answered"], steps[0]["dropped"]), (2, 2, 0))
        self.assertEqual((steps[1]["viewed"], steps[1]["answered"], steps[1]["dropped"], steps[1]["drop_rate"]),
                         (2, 1, 1, 0.5))

    def test_by_store_by_day_and_what_was_searched(self):
        r = _post(self.URL, {}).json()
        self.assertEqual({k: v["sessions"] for k, v in r["by_store"].items()},
                         {"pullman": 1, "mount-vernon": 2, "yakima": 1})
        self.assertEqual(r["by_store"]["mount-vernon"]["bounces"], 2)
        self.assertEqual(sum(d["sessions"] for d in r["by_day"]), 4)
        cats = {c["category"]: c["searches"] for c in r["top_categories"]}
        self.assertEqual(cats, {"flower": 2, "edible": 1, "vape": 1})
        self.assertIn({"slots": "category=edible · size=100mg", "searches": 1}, r["zero_result_searches"])
        self.assertTrue(any(s["slot"] == "effect=sleep" for s in r["top_slots"]))

    def test_filter_to_one_store(self):
        r = _post(self.URL, {"store": "mt vernon"}).json()
        self.assertEqual((r["store"], r["sessions"]), ("mount-vernon", 2))

    def test_staff_token_only_and_no_secrets_in_the_response(self):
        self.assertIn(_post(self.URL, {}, WEBSITE).status_code, (401, 403))
        self.assertIn(_post("/api/v1/analytics/session", {"ref": "0" * 16}, WEBSITE).status_code, (401, 403))
        text = json.dumps(_post(self.URL, {}).json())
        for token in ("s-A", "s-B", "s-C", "s-D"):
            self.assertNotIn(f'"{token}"', text)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class TimelineTests(TestCase):
    def test_events_messages_and_picks_come_back_in_the_order_they_happened(self):
        chat = ChatSession.objects.create(session_token="s-T1", location_slug="pullman", channel="chat")
        m1 = ChatMessage.objects.create(session=chat, role="user", content="something for sleep")
        m2 = ChatMessage.objects.create(session=chat, role="assistant", content="Try these")
        pick = SuggestedProduct.objects.create(session=chat, location_slug="pullman", sku="SKU1")
        for obj, secs in ((m1, 20), (m2, 30)):
            ChatMessage.objects.filter(pk=obj.pk).update(ts=NOW + timedelta(seconds=secs))
        SuggestedProduct.objects.filter(pk=pick.pk).update(shown_at=NOW + timedelta(seconds=40))
        for n, secs in (("chat_open", 0), ("search_run", 35), ("product_card_click", 50), ("nav_away", 60)):
            e = AnalyticsEvent.objects.create(session_token="s-T1", event_type=n, location_slug="pullman")
            AnalyticsEvent.objects.filter(pk=e.pk).update(ts=NOW + timedelta(seconds=secs))
        ref = analytics.session_ref("s-T1")
        by_ref = _post("/api/v1/analytics/session", {"ref": ref}).json()
        by_id = _post("/api/v1/analytics/session", {"id": chat.pk}).json()
        self.assertEqual(by_ref["id"], by_id["id"])
        kinds = [(i["kind"], i.get("name") or i.get("role") or i.get("sku")) for i in by_ref["timeline"]]
        self.assertEqual(kinds, [("event", "chat_open"), ("message", "user"), ("message", "assistant"),
                                 ("event", "search_run"), ("suggestion", "SKU1"), ("event", "product_card_click"),
                                 ("event", "nav_away")])
        self.assertEqual([i["t"] for i in by_ref["timeline"]], [0, 20, 30, 35, 40, 50, 60])
        self.assertEqual(by_ref["counts"], {"events": 4, "messages": 2, "suggestions": 1})
        self.assertEqual(by_ref["outcome"], "clicked_product")
        self.assertNotIn("s-T1", json.dumps(by_ref))

    def test_unknown_session_is_404(self):
        self.assertEqual(_post("/api/v1/analytics/session", {"ref": "f" * 16}).status_code, 404)
        self.assertEqual(_post("/api/v1/analytics/session", {"id": 99999}).status_code, 404)
        self.assertEqual(_post("/api/v1/analytics/session", {}).status_code, 404)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class ConversationsAreKeptTests(TestCase):
    TOKEN = "s-" + "p" * 24

    def _persist(self, messages):
        return _post("/api/v1/chat/persist/", {"session_id": self.TOKEN, "stage": "RESULTS",
                                               "slots": {"store": "pullman"}, "messages": messages}, WEBSITE)

    def setUp(self):
        ChatSession.objects.create(session_token=self.TOKEN)

    def test_persisting_the_same_snapshot_twice_stores_it_once(self):
        snap = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello", "chips": ["Flower"]}]
        self._persist(snap)
        self._persist(snap)
        self.assertEqual(ChatMessage.objects.count(), 2)

    def test_a_longer_snapshot_only_adds_the_new_messages(self):
        a = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        self._persist(a)
        first_ids = list(ChatMessage.objects.values_list("id", flat=True))
        self._persist(a + [{"role": "user", "content": "flower please"}])
        self.assertEqual(ChatMessage.objects.count(), 3)
        self.assertEqual(list(ChatMessage.objects.values_list("id", flat=True))[:2], first_ids)

    def test_a_restarted_or_shorter_snapshot_never_deletes_what_was_stored(self):
        self._persist([{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"},
                       {"role": "user", "content": "three"}])
        self._persist([{"role": "user", "content": "fresh start"}])  # "Start over" reusing the session
        self.assertEqual(sorted(ChatMessage.objects.values_list("content", flat=True)),
                         ["fresh start", "one", "three", "two"])

    def test_a_late_result_list_is_filled_into_the_stored_message(self):
        self._persist([{"role": "assistant", "content": "picks"}])
        self._persist([{"role": "assistant", "content": "picks", "search_results": [{"sku": "S1"}]}])
        self.assertEqual(ChatMessage.objects.get().result_skus, ["S1"])

    def test_the_reset_command_refuses_to_wipe_conversations_without_the_override(self):
        self._persist([{"role": "user", "content": "keep me"}])
        with self.assertRaises(CommandError):
            call_command("reset_analytics", "--yes")
        self.assertEqual(ChatMessage.objects.count(), 1)


class VisitorBackfillTests(TestCase):
    def test_the_migration_lifts_props_visitor_id_into_the_column(self):
        import importlib

        mig = importlib.import_module("budtender.migrations.0012_durability_indexes_visitor_id")
        AnalyticsEvent.objects.create(event_type="chat_open", props={"visitor_id": "abc123"})
        AnalyticsEvent.objects.create(event_type="chat_open", props={})
        mig.backfill_visitor_id(django_apps, None)
        self.assertEqual(sorted(AnalyticsEvent.objects.values_list("visitor_id", flat=True)), ["", "abc123"])


class PruneSiteNoiseTests(TestCase):
    def test_only_old_performance_beacons_go_never_chat_events_or_conversations(self):
        old = NOW - timedelta(days=200)
        keep_old = [AnalyticsEvent.objects.create(event_type=n) for n in ("chat_open", "search_run", "phone_click", "feedback")]
        gone = [AnalyticsEvent.objects.create(event_type=n) for n in ("web_vital", "scroll", "time_on_page")]
        recent = AnalyticsEvent.objects.create(event_type="web_vital")
        AnalyticsEvent.objects.filter(pk__in=[e.pk for e in keep_old + gone]).update(ts=old)
        chat = ChatSession.objects.create(session_token="s-keep")
        ChatMessage.objects.create(session=chat, role="user", content="keep")
        call_command("prune_site_noise", "--dry-run")
        self.assertEqual(AnalyticsEvent.objects.count(), 8)
        call_command("prune_site_noise", "--days", "90")
        self.assertEqual(sorted(AnalyticsEvent.objects.values_list("event_type", flat=True)),
                         sorted(["chat_open", "search_run", "phone_click", "feedback", "web_vital"]))
        self.assertTrue(AnalyticsEvent.objects.filter(pk=recent.pk).exists())
        self.assertEqual((ChatSession.objects.count(), ChatMessage.objects.count()), (1, 1))


@override_settings(HHT_BACKEND_TOKEN=BACKEND, CACHES=_LOCMEM)
class HistoryPagingTests(TestCase):
    def test_the_list_pages_through_every_conversation(self):
        for i in range(5):
            s = ChatSession.objects.create(session_token=f"s-h{i}")
            ChatMessage.objects.create(session=s, role="user", content=f"m{i}")
        first = _post("/api/v1/chat/history", {"limit": 2}).json()
        second = _post("/api/v1/chat/history", {"limit": 2, "offset": 2}).json()
        self.assertEqual((first["total"], len(first["sessions"]), len(second["sessions"])), (5, 2, 2))
        self.assertEqual(first["sessions"][0]["message_count"], 1)
        self.assertFalse({r["id"] for r in first["sessions"]} & {r["id"] for r in second["sessions"]})
