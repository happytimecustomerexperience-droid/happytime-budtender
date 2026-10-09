"""Backend hardening (2026-10-08 adversarial review): what a website request body can and cannot do.

Every test here is a request a shopper's input (or a leaked website token) can produce. Each one used
to 500, leak, or cost more than it should; each now gets a bounded answer.
"""
import inspect
import json
import re
import time
from unittest import mock

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from budtender import facets, ranking, views
from budtender.models import AnalyticsEvent, ChatMessage, ChatSession, Feedback, Product, SyncState

BACKEND = "backend-token"
WEBSITE = "website-token"
LOC = "yakima"


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE)
class _Api(TestCase):
    @classmethod
    def setUpTestData(cls):
        SyncState.objects.update_or_create(location_slug=LOC, defaults={"last_synced_at": timezone.now()})
        for i, (margin, name) in enumerate(((5, "Cheap Margin Kush"), (50, "Fat Margin Haze"))):
            Product.objects.create(sku=f"M{i}", product_id=f"80{i}", batch_id=f"70{i}", location_slug=LOC,
                                   name=name, category="flower", price=30, cost=30 - margin, margin=margin,
                                   quantity_on_hand=10, availability=True, slug=f"m{i}")

    def raw(self, path, body: str, token=WEBSITE):
        return Client().post(f"/api/v1/{path}", data=body, content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {token}")

    def post(self, path, body, token=WEBSITE):
        return self.raw(path, json.dumps(body), token)


class MalformedBodyTests(_Api):
    def test_deeply_nested_json_is_a_400_not_a_500(self):
        for path in ("track/", "products/search/", "chat/persist/", "products/facets"):
            r = self.raw(path, "[" * 50000 + "]" * 50000)
            self.assertEqual(r.status_code, 400, path)
            r = self.post(path, {"event_type": "chat_open", "props": json.loads('{"a":' * 200 + "1" + "}" * 200)})
            self.assertEqual(r.status_code, 400, path)

    def test_a_top_level_list_or_string_is_a_400_not_a_500(self):
        for path in ("pairing/for-sku", "products/search/", "chat/persist/", "feedback/", "track/",
                     "customer/session-context", "products/similar"):
            for body in ("[1, 2]", '"hi"', "7"):
                self.assertEqual(self.raw(path, body).status_code, 400, (path, body))

    def test_a_parse_error_says_nothing_about_the_server(self):
        body = self.raw("track/", "[" * 50000).json()
        self.assertEqual(set(body), {"detail"})
        self.assertNotRegex(json.dumps(body).lower(), r"traceback|settings|secret|/home/|\.py")

    def test_nul_bytes_never_reach_the_database(self):
        # Postgres text/jsonb refuse U+0000: one in a typed message was a 500 at the INSERT.
        self.assertEqual(self.post("feedback/", {"message": "great\u0000 staff", "rating": 5}).status_code, 201)
        self.assertEqual(Feedback.objects.get().message, "great staff")
        token = "s-" + "n" * 20
        self.post("chat/persist/", {"session_token": token, "messages": [{"role": "user", "content": "a\u0000b"}]})
        self.assertEqual(ChatMessage.objects.get(session__session_token=token).content, "ab")
        self.post("track/", {"event_type": "chat_open", "props": {"k\u0000": "v\u0000"}})
        self.assertEqual(AnalyticsEvent.objects.get(event_type="chat_open").props, {"k": "v"})

    def test_a_feedback_rating_outside_1_to_5_is_dropped_not_a_db_error(self):
        for rating in (10 ** 30, -1, 0, 6):
            self.assertEqual(self.post("feedback/", {"message": "x", "rating": rating}).status_code, 201)
        self.assertEqual(set(Feedback.objects.values_list("rating", flat=True)), {None})

    def test_persist_takes_only_an_object_for_slots_and_a_short_stage(self):
        token = "s-" + "p" * 20
        self.assertEqual(self.post("chat/persist/", {"session_token": token, "slots": [1, 2]}).status_code, 202)
        self.post("chat/persist/", {"session_token": token, "stage": {"x": 1}})
        self.post("chat/persist/", {"session_token": token, "slots": {"store": LOC, "pad": "x" * 20000}})
        s = ChatSession.objects.get(session_token=token)
        self.assertEqual((s.stage, s.slots), ("WELCOME", {}))
        self.post("chat/persist/", {"session_token": token, "stage": "RESULTS" + "X" * 100, "slots": {"store": LOC}})
        s.refresh_from_db()
        self.assertEqual((len(s.stage), s.slots), (24, {"store": LOC}))


class SearchInputTests(_Api):
    def _spy(self):
        seen = []

        def spy(location, slots, profile, **kw):
            seen.append({"slots": slots, **kw})
            return []
        return seen, mock.patch("budtender.views.rank_products", side_effect=spy)

    def test_the_website_token_cannot_rank_by_margin(self):
        """Weights {margin: 1, everything else: 0} would order the results by margin alone, and the
        caller would read the store's margin order off them. Only the voice service forwards weights."""
        by_margin = {"w_anon": {"margin": 1, "affinity": 0, "effect": 0, "category": 0, "bucket": 0, "quality": 0,
                               "budget": 0}, "margin_emphasis": 1e6}
        seen, patch = self._spy()
        with patch:
            self.post("products/search/", {"slots": {"store": LOC}, "ranking_weights": by_margin}, WEBSITE)
            self.post("products/search/", {"slots": {"store": LOC}, "ranking_weights": by_margin}, BACKEND)
        self.assertIsNone(seen[0]["ranking_weights"])
        self.assertEqual(seen[-1]["ranking_weights"], by_margin)  # control: the voice service's levers

    def test_exclude_skus_is_a_bounded_list(self):
        seen, patch = self._spy()
        with patch:
            self.post("products/search/", {"slots": {}, "exclude_skus": [f"x{i}" * 50 for i in range(5000)]})
            self.post("products/search/", {"slots": {}, "exclude_skus": "M0"})
        self.assertLessEqual(len(seen[0]["exclude_skus"]), views._EXCLUDE_SKUS_MAX)
        self.assertTrue(all(len(s) <= 64 for s in seen[0]["exclude_skus"]))
        self.assertEqual(seen[-1]["exclude_skus"], set())   # a string is not a list of one-letter skus

    def test_slots_reach_the_ranker_bounded(self):
        seen, patch = self._spy()
        huge = {f"k{i}": "v" for i in range(500)}
        huge.update({"q": "x" * 300000, "tags": ["t"] * 100000, "nested": {"a": {"b": 1}}, "category": "flower"})
        with patch:
            self.post("products/search/", {"slots": huge})
        slots = seen[0]["slots"]
        self.assertLessEqual(len(slots), views._SLOT_KEYS_MAX)
        self.assertNotIn("nested", slots)
        self.assertTrue(all(len(v) <= views._SLOT_STR_MAX for v in slots.values() if isinstance(v, str)))
        self.assertTrue(all(len(v) <= views._SLOT_LIST_MAX for v in slots.values() if isinstance(v, list)))

    def test_v2_free_text_fields_are_data_not_code(self):
        """q / tags / terpenes / infusion / subcategory never reach SQL or a regex: an injection, a LIKE or
        regex wildcard or a catastrophic-backtracking pattern is a plain string. A hard filter given one
        matches nothing; a `q` with no 2-char token is no filter at all (by design), never "match all"."""
        hostile = ["' OR 1=1 --", "%", "%%", "_", "(a+)+$", ".*", "\\", "]]]]", "{{7*7}}",
                   "<script>alert(1)</script>", "M0' UNION SELECT cost FROM budtender_product --"]
        unfiltered = {r["sku"] for r in self.post("products/search/", {"slots": {"store": LOC}, "limit": 20}).json()["results"]}
        self.assertEqual(unfiltered, {"M0", "M1"})
        for value in hostile:
            for key, slot in (("q", value), ("tags", [value]), ("terpenes", [value]), ("infusion", value),
                              ("subcategory", value), ("category", value)):
                r = self.post("products/search/", {"slots": {"store": LOC, key: slot}, "limit": 20})
                self.assertEqual(r.status_code, 200, (key, value))
                got = r.json()["results"]
                self.assertNotRegex(json.dumps(got), r'"(cost|margin)"')
                # a value that normalises to nothing / no known terpene is ignored; otherwise it filters
                self.assertIn({g["sku"] for g in got}, (set(), unfiltered), (key, value))
                wildcard = value in ("%", "%%", ".*", "(a+)+$", "' OR 1=1 --")
                if (key in ("tags", "subcategory", "category", "infusion") and wildcard) or \
                        (key == "q" and value in ("%%", "' OR 1=1 --")):
                    self.assertEqual(got, [], (key, value))
                r = self.post("products/facets", {"store": LOC, "slots": {key: slot}, "facet": "tags"})
                self.assertEqual(r.status_code, 200, (key, value))
        self.assertEqual(Product.objects.count(), 2)  # nothing was rewritten either

    def test_a_maximal_body_of_filters_is_still_cheap(self):
        slots = {"store": LOC, "q": "kush " * 20000, "tags": ["a" * 20000] * 20,      # ~1.4 MB, under the
                 "terpenes": ["b" * 20000] * 20, "infusion": ["c" * 20000] * 20,  # 2.5 MB body cap
                 "subcategory": "d" * 100000}
        started = time.monotonic()
        for path in ("products/search/", "products/specify-more", "products/categories"):
            self.assertEqual(self.post(path, {"slots": slots}).status_code, 200, path)
        self.assertLess(time.monotonic() - started, 5)


class FacetCacheKeyTests(_Api):
    def test_junk_keys_and_padding_cannot_mint_new_cache_entries(self):
        calls = []
        real = facets._facet

        def counting(ctx, slots, name):
            calls.append(name)
            return real(ctx, slots, name)

        with mock.patch("budtender.facets._facet", side_effect=counting):
            for i in range(25):
                facets.facet(LOC, {"category": "flower", f"junk{i}": i, "q": "kush" + " " * i + "#" * 100 * i}, "tags")
        self.assertEqual(len(calls), 1)

    def test_a_real_filter_still_gets_its_own_entry(self):
        a = facets.cache_key_slots({"category": "flower"})
        for other in ({"category": "concentrates"}, {"category": "flower", "doh_only": True},
                      {"category": "flower", "q": "haze"}, {"category": "flower", "price_min": 10}):
            self.assertNotEqual(a, facets.cache_key_slots(other), other)

    def test_the_key_covers_every_slot_the_filters_read(self):
        """If ranking grows a new hard filter, it must be added to CACHE_KEY_RAW_SLOTS (or parsed in
        parse_filters), or two different questions would share one cached answer."""
        read = set()
        for fn in (ranking.eligible, ranking.price_window, ranking.effective_size):
            read |= set(re.findall(r"slots\.get\(\"(\w+)\"", inspect.getsource(fn)))
        parsed = set(re.findall(r"slots\.get\(\"(\w+)\"", inspect.getsource(ranking.parse_filters)))
        self.assertTrue(read)
        self.assertLessEqual(read - parsed, set(facets.CACHE_KEY_RAW_SLOTS))


class ChatInputTests(_Api):
    def test_a_huge_message_is_capped_before_the_intent_regexes_see_it(self):
        seen = []
        with mock.patch("budtender.views.generate_chat_reply_with_source", return_value=("hi", "brain", "")), \
             mock.patch("budtender.views.classify_intent", side_effect=lambda m, b=None: seen.append(m) or "other"):
            r = self.post("chat/message", {"message": "a " * 1_000_000, "location": LOC})
        self.assertEqual(r.status_code, 200)
        self.assertLessEqual(len(seen[0]), 4000)


def test_the_budtender_image_never_bakes_in_the_voice_secrets():
    """The root build context includes voice/; a bare ".env" in .dockerignore matches only ./.env, so
    voice/.env and voice/secrets/*.json were copied into every budtender image layer."""
    from pathlib import Path

    lines = {ln.strip() for ln in (Path(__file__).resolve().parents[2] / ".dockerignore").read_text().splitlines()}
    for pattern in ("**/.env", "**/.env.*", "voice/secrets/", ".env", ".env.dutchie", "**/stores.json"):
        assert pattern in lines, pattern
