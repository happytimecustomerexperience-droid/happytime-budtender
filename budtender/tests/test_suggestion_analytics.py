"""Suggestion analytics v1 (docs/contracts/suggestion-analytics-v1.md).

Every suggestion is stored with its full customer-facing card (never cost/margin), and the transaction
ingest decides, for 10 days, whether the customer bought it or a sibling. Dutchie is mocked; offline.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone as dj_tz

from budtender import identity, suggestions, tasks
from budtender.models import (ChatSession, CustomerProfile, Product, SuggestedProduct,
                              SuggestionOutcome)
from budtender.serializers import public_product

BACKEND, WEBSITE = "backend-token", "website-token"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PHONE = "+15095551212"
CUSTOMERS = [{"customerId": "1", "cellPhone": "509-555-1212", "firstName": "Jane", "lastName": "Doe"}]
TOKEN = "s-" + "Qw3rTy7uIo" * 3


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    monkeypatch.setattr("budtender.views.fire", lambda *a, **k: False)


def _post(path, payload, token=BACKEND):
    return Client().post(path, data=json.dumps(payload), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


def _product(sku, name, brand, category, *, pid="", strain="", price=20, loc="yakima", **kw):
    return Product.objects.create(
        sku=sku, product_id=pid or f"P-{sku}", location_slug=loc, slug=f"{sku.lower()}-slug", name=name,
        brand=brand, category=category, strain=strain, strain_type=kw.pop("strain_type", "hybrid"),
        price=price, cost=kw.pop("cost", 7.77), margin=kw.pop("margin", 12.23), quantity_on_hand=20,
        availability=True, thc_percent=kw.pop("thc_percent", 22.5), **kw)


def _catalog():
    return {
        "bon10": _product("BON10", "Verdelux DOH Approved Bon Bombs 1:1 CBD Classic 10pk", "Verdelux", "edibles",
                          price=18),
        "bon20": _product("BON20", "Verdelux DOH Approved Bon Bombs 1:1 CBD Classic 20pk", "Verdelux", "edibles",
                          price=32),
        "blush_sk": _product("BLSK", "Blush Velvet Gummies Strawberry Kiwi 20pk", "Blush", "edibles"),
        "blush_wm": _product("BLWM", "Blush Velvet Gummies Watermelon 20pk", "Blush", "edibles"),
        "bd35": _product("BD35", "Phat Panda - Blue Dream - 3.5g", "Phat Panda", "flower", strain="Blue Dream",
                         unit_weight=3.5),
        "wc28": _product("WC28", "Phat Panda - Wedding Cake - 28g", "Phat Panda", "flower", strain="Wedding Cake",
                         unit_weight=28),
        "wyld": _product("WYLD", "Wyld Gummies Strawberry 10pk", "Wyld", "edibles"),
    }


def _suggest(p, customer=None, *, shown_at=None, session=None, channel="chat", kind="primary"):
    row = suggestions.record(session=session, customer=customer, location=p.location_slug,
                             picks=[(p, public_product(p, rank=1, why_this="fits"))], kind=kind, channel=channel,
                             identity_via="caller_id" if customer else "")[0]
    if shown_at is not None:
        SuggestedProduct.objects.filter(pk=row.pk).update(shown_at=shown_at)
        SuggestionOutcome.objects.filter(suggestion=row).update(window_ends_at=shown_at + timedelta(days=10))
    return row


def _outcome(row) -> SuggestionOutcome:
    return SuggestionOutcome.objects.get(suggestion_id=row.pk)


def _tx(when, pid, tx_id, qty=1, price=10.0):
    return {"customerId": "1", "transactionId": tx_id, "transactionDate": when.isoformat(),
            "items": [{"productId": pid, "quantity": qty, "unitPrice": price}]}


# ── sibling key ──────────────────────────────────────────────────────────────
class SiblingKeyTests(SimpleTestCase):
    def _kind(self, a, b):
        return suggestions.match_kind({**a, "sku": "A"}, {**b, "sku": "B"})

    def test_same_line_other_pack_size_is_a_size_sibling(self):
        a = dict(name="Verdelux DOH Approved Bon Bombs 1:1 CBD Classic 10pk", brand="Verdelux", category="edibles")
        b = dict(a, name="Verdelux DOH Approved Bon Bombs 1:1 CBD Classic 20pk")
        self.assertEqual(suggestions.sibling_key(**a), "verdelux|edible|bon bombs 1:1 cbd")
        self.assertEqual(suggestions.sibling_key(**a), suggestions.sibling_key(**b))
        self.assertEqual(self._kind(a, b), "sibling_size")

    def test_same_line_other_flavour_is_a_strain_sibling(self):
        a = dict(name="Blush Velvet Gummies Strawberry Kiwi 20pk", brand="Blush", category="edibles")
        b = dict(a, name="Blush Velvet Gummies Watermelon 20pk")
        self.assertEqual(suggestions.sibling_key(**a), "blush|edible|velvet gummies")
        self.assertEqual(self._kind(a, b), "sibling_strain")

    def test_other_strain_and_size_is_both(self):
        a = dict(name="Phat Panda - Blue Dream - 3.5g", brand="Phat Panda", category="flower", strain="Blue Dream")
        b = dict(name="Phat Panda - Wedding Cake - 28g", brand="Phat Panda", category="flower", strain="Wedding Cake")
        self.assertEqual(suggestions.sibling_key(**a), suggestions.sibling_key(**b))
        self.assertEqual(self._kind(a, b), "sibling_both")

    def test_cart_sizes_and_spellings(self):
        a = dict(name="Agro Couture Oregon Diesel Live Resin Cart 1g", brand="Agro Couture",
                 category="vape-cartridges", strain="Oregon Diesel")
        b = dict(a, name="Agro Couture Oregon Diesel Live Resin Cartridge 0.5 g")
        self.assertEqual(suggestions.sibling_key(**a), "agro couture|vape|live resin cart")
        self.assertEqual(self._kind(a, b), "sibling_size")
        self.assertEqual(suggestions.size_signature(b["name"]), frozenset({"0.5g"}))
        self.assertEqual(suggestions.size_signature("Sticky Frog Pre-Roll (2pk) 2x0.5g"),
                         frozenset({"2pk", "2x0.5g"}))
        self.assertEqual(suggestions.size_signature("Gummies 100mg 10 pack"), frozenset({"100mg", "10pk"}))

    def test_different_brand_is_never_a_sibling(self):
        a = dict(name="Wyld Gummies Strawberry 10pk", brand="Wyld", category="edibles")
        b = dict(name="Blush Velvet Gummies Strawberry 10pk", brand="Blush", category="edibles")
        self.assertNotEqual(suggestions.sibling_key(**a), suggestions.sibling_key(**b))
        self.assertEqual(self._kind(a, b), "")
        # same name, different brand -> still not
        self.assertEqual(self._kind(a, dict(a, brand="Wyld Canada")), "")

    def test_different_line_or_formula_or_category_is_not_a_sibling(self):
        e = "edibles"
        self.assertEqual(self._kind(dict(name="Verdelux Bon Bombs 1:1 CBD 10pk", brand="Verdelux", category=e),
                                    dict(name="Verdelux Bon Bombs 20:1 CBD 10pk", brand="Verdelux", category=e)), "")
        self.assertEqual(self._kind(dict(name="Wyld CBN Gummies Elderberry 10pk", brand="Wyld", category=e),
                                    dict(name="Wyld Gummies Elderberry 10pk", brand="Wyld", category=e)), "")
        self.assertEqual(self._kind(
            dict(name="Sticky Frog Infused Pre-Roll Jack Herer 1g", brand="Sticky Frog", category="pre-rolls",
                 strain="Jack Herer"),
            dict(name="Sticky Frog Pre-Roll Jack Herer 1g", brand="Sticky Frog", category="pre-rolls",
                 strain="Jack Herer")), "")
        self.assertEqual(self._kind(
            dict(name="Phat Panda Blue Dream 1g", brand="Phat Panda", category="flower", strain="Blue Dream"),
            dict(name="Phat Panda Blue Dream Live Resin Cart 1g", brand="Phat Panda", category="vape-cartridges",
                 strain="Blue Dream")), "")

    def test_no_brand_never_matches_and_same_product_other_sku_is_exact(self):
        self.assertEqual(suggestions.sibling_key("Blue Dream 3.5g", "", "flower"), "")
        a = dict(name="Phat Panda - Blue Dream - 3.5g", brand="Phat Panda", category="flower", strain="Blue Dream")
        self.assertEqual(self._kind(a, dict(a)), "exact")  # another store / re-SKU of the same product
        self.assertEqual(suggestions.match_kind({"sku": "X", "product_id": "9"}, {"sku": "Y", "product_id": "9"}),
                         "exact")


class ChannelTests(SimpleTestCase):
    def test_phone_only_from_the_backend(self):
        call = ChatSession(session_token="vc-abc123", channel="voice")
        web = ChatSession(session_token=TOKEN, channel="questionnaire")
        rc = suggestions.resolve_channel
        self.assertEqual(rc(None, call, website=False), "phone")
        self.assertEqual(rc("chat", call, website=False), "phone")
        self.assertEqual(rc("phone", None, website=False), "phone")
        self.assertEqual(rc(None, None, website=False, caller_id=True), "phone")
        self.assertEqual(rc("phone", web, website=True), "questionnaire")      # the site cannot claim phone
        self.assertEqual(rc(None, ChatSession(channel="voice"), website=True), "unknown")
        self.assertEqual(rc("questionnaire", None, website=True), "questionnaire")
        self.assertEqual(rc("<script>", web, website=True), "questionnaire")   # off-allowlist -> session
        self.assertEqual(rc(None, None, website=True, default="similar"), "similar")
        self.assertEqual(rc(None, None, website=True), "unknown")


# ── recording on every path ──────────────────────────────────────────────────
@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM, HHT_WEB_PHONE_IDENTITY=True)
class RecordingTests(TestCase):
    def setUp(self):
        self.c = _catalog()

    def _search(self, payload, token=WEBSITE, picks=None):
        picks = picks if picks is not None else [(self.c["bd35"], "your strain"), (self.c["bon10"], "on deal")]
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products", return_value=picks):
            return _post("/api/v1/products/search/", {"slots": {"store": "yakima"}, **payload}, token)

    def _assert_snapshot(self, row, p, price):
        snap = row.snapshot
        for k, v in (("name", p.name), ("brand", p.brand), ("category", p.category), ("product_id", p.product_id),
                     ("slug", p.slug), ("strain_type", p.strain_type)):
            self.assertEqual(snap[k], v, k)
        self.assertEqual(snap["price"], price)
        self.assertEqual(snap["thc_percent"], 22.5)
        self.assertEqual(set(snap) - set(suggestions.SNAPSHOT_FIELDS), set())
        blob = json.dumps(snap).lower()
        for word in ("cost", "margin", "7.77", "12.23"):
            self.assertNotIn(word, blob)

    def test_questionnaire_search_writes_full_snapshots_and_pending_outcomes(self):
        r = self._search({"session_token": TOKEN, "source": "questionnaire"})
        self.assertEqual(r.status_code, 200)
        rows = list(SuggestedProduct.objects.order_by("id"))
        self.assertEqual([x.sku for x in rows], ["BD35", "BON10"])
        bd, bon = rows
        self._assert_snapshot(bd, self.c["bd35"], 20.0)
        self.assertEqual((bd.snapshot["strain"], bd.snapshot["size_label"], bd.snapshot["rank"], bd.snapshot["why"]),
                         ("Blue Dream", "3.5g", 1, "your strain"))
        self.assertEqual(bd.snapshot["unit_weight"], 3.5)
        self.assertEqual((bon.snapshot["rank"], bon.snapshot["kind"], bon.snapshot["source"]), (2, "primary",
                                                                                                 "questionnaire"))
        self.assertEqual(bon.sibling_key, "verdelux|edible|bon bombs 1:1 cbd")
        self.assertEqual(bon.snapshot["size_label"], "10pk")  # edible: the pack size from the name
        self.assertEqual({x.channel for x in rows}, {"questionnaire"})
        self.assertIsNone(bd.customer_id)
        o = _outcome(bd)
        self.assertEqual(o.status, "pending")
        self.assertEqual(o.window_ends_at - bd.shown_at, timedelta(days=10))

    def test_recording_is_two_inserts_whatever_the_page_size(self):
        """No query per suggested row: 2 picks and 5 picks cost the same number of queries."""
        many = [(p, "x") for p in self.c.values()][:5]
        self._search({"session_token": TOKEN, "source": "chat"})  # session exists from here on
        with CaptureQueriesContext(connection) as two:
            self._search({"session_token": TOKEN, "source": "chat"})
        with CaptureQueriesContext(connection) as five:
            self._search({"session_token": TOKEN, "source": "chat"}, picks=many)
        self.assertEqual(len(two), len(five))
        self.assertEqual(SuggestedProduct.objects.count(), 2 + 2 + 5)
        self.assertEqual(SuggestionOutcome.objects.count(), 9)

    def test_the_website_cannot_label_a_suggestion_phone(self):
        self._search({"session_token": TOKEN, "source": "phone"})
        self.assertEqual(set(SuggestedProduct.objects.values_list("channel", flat=True)), {"questionnaire"})

    def test_a_phone_call_search_is_channel_phone_with_the_caller(self):
        caller = CustomerProfile.objects.create(phone=PHONE, name="Jane Doe", total_orders=2)
        ChatSession.objects.create(session_token="vc-call-77", channel="voice", customer=caller,
                                   identity_via="caller_id", phone=PHONE)
        self._search({"session_token": "vc-call-77", "phone": PHONE}, token=BACKEND)
        rows = SuggestedProduct.objects.all()
        self.assertEqual({(x.channel, x.customer_id, x.identity_via) for x in rows}, {("phone", caller.pk, "caller_id")})

    def test_an_anonymous_call_labelled_phone_is_recorded_without_a_session(self):
        self._search({"source": "phone"}, token=BACKEND)
        rows = list(SuggestedProduct.objects.all())
        self.assertEqual(len(rows), 2)
        self.assertEqual({(x.channel, x.session_id, x.customer_id) for x in rows}, {("phone", None, None)})

    def test_pairing_records_a_pairing_with_its_card_and_reason(self):
        pair = self.c["bon20"]
        with patch("budtender.views.pair_for", return_value=(pair, "copurchase", "People pair these", 0.8)):
            r = _post("/api/v1/pairing/for-sku", {"location": "yakima", "sku": "BD35", "session_token": TOKEN},
                      WEBSITE)
        self.assertEqual(r.json()["pairing"]["sku"], "BON20")
        row = SuggestedProduct.objects.get()
        self.assertEqual((row.kind, row.channel, row.paired_with_sku, row.reason_code), ("pairing", "pairing", "BD35",
                                                                                         "copurchase"))
        self._assert_snapshot(row, pair, 32.0)
        self.assertEqual(row.snapshot["why"], "People pair these")
        self.assertEqual(_outcome(row).status, "pending")

    def test_find_similar_records_channel_similar(self):
        page = [(self.c["wc28"], "same type"), (self.c["blush_sk"], "")]
        with patch("budtender.product_similarity.similar_products", return_value=page):
            r = _post("/api/v1/products/similar", {"store": "yakima", "sku": "BD35", "session_token": TOKEN}, WEBSITE)
        self.assertEqual(len(r.json()["results"]), 2)
        rows = list(SuggestedProduct.objects.order_by("id"))
        self.assertEqual([(x.sku, x.channel, x.paired_with_sku) for x in rows],
                         [("WC28", "similar", "BD35"), ("BLSK", "similar", "BD35")])
        self._assert_snapshot(rows[0], self.c["wc28"], 20.0)
        self.assertEqual(rows[0].snapshot["size_label"], "28g")

    def test_a_web_phone_session_records_its_customer_and_identity_via(self):
        jane = CustomerProfile.objects.create(phone=PHONE, name="Jane Doe", total_orders=2)
        ChatSession.objects.create(session_token=TOKEN, channel="chat", customer=jane, identity_via="web_phone",
                                   phone=PHONE)
        self._search({"session_token": TOKEN, "source": "chat"})
        self.assertEqual({(x.customer_id, x.identity_via, x.channel) for x in SuggestedProduct.objects.all()},
                         {(jane.pk, "web_phone", "chat")})


# ── attribution ──────────────────────────────────────────────────────────────
@override_settings(CACHES=_LOCMEM)
@patch("budtender.tasks.classify_products", lambda *a, **k: None)
@patch("budtender.dutchie.get_customers", lambda slug: CUSTOMERS)
class AttributionTests(TestCase):
    def setUp(self):
        self.c = _catalog()
        self.jane = CustomerProfile.objects.create(phone=PHONE, name="Jane Doe")
        self.t0 = dj_tz.now() - timedelta(days=4)

    def _sync(self, txs, *, rebuild=False):
        with patch("budtender.dutchie.get_transactions_detailed",
                   lambda slug, *a, **k: list(txs) if slug == "yakima" else []):
            if rebuild:
                call_command("rebuild_customer_history")
            else:
                tasks.sync_transactions("yakima")

    def test_exact_purchase_within_the_window(self):
        s = _suggest(self.c["bon10"], self.jane, shown_at=self.t0)
        self._sync([_tx(self.t0 + timedelta(days=1), "P-BON10", "T1", qty=2, price=18.0)])
        o = _outcome(s)
        self.assertEqual((o.status, o.match_kind, o.matched_sku), ("bought_exact", "exact", "BON10"))
        self.assertEqual(float(o.matched_amount), 36.0)
        self.assertEqual(o.matched_name, self.c["bon10"].name)
        self.assertEqual(o.matched_line, "T1:P-BON10:0")

    def test_siblings_by_size_strain_and_both(self):
        size = _suggest(self.c["bon10"], self.jane, shown_at=self.t0)
        strain = _suggest(self.c["blush_sk"], self.jane, shown_at=self.t0)
        both = _suggest(self.c["bd35"], self.jane, shown_at=self.t0)
        t = self.t0 + timedelta(days=2)
        self._sync([_tx(t, "P-BON20", "A"), _tx(t, "P-BLWM", "B"), _tx(t, "P-WC28", "C")])
        self.assertEqual((_outcome(size).status, _outcome(size).match_kind), ("bought_sibling", "sibling_size"))
        self.assertEqual(_outcome(strain).match_kind, "sibling_strain")
        self.assertEqual(_outcome(both).match_kind, "sibling_both")
        self.assertEqual(_outcome(both).matched_name, "Phat Panda - Wedding Cake - 28g")

    def test_a_different_brand_is_not_a_conversion(self):
        s = _suggest(self.c["blush_sk"], self.jane, shown_at=self.t0)
        self._sync([_tx(self.t0 + timedelta(days=1), "P-WYLD", "W")])
        self.assertEqual(_outcome(s).status, "pending")

    def test_purchases_before_shown_at_or_after_the_window_do_not_count(self):
        s = _suggest(self.c["bon10"], self.jane, shown_at=self.t0 - timedelta(days=20))
        self._sync([_tx(self.t0 - timedelta(days=21), "P-BON10", "EARLY"),
                    _tx(self.t0 - timedelta(days=9), "P-BON10", "LATE")])  # shown+11d: past the 10-day window
        self.assertEqual(_outcome(s).status, "pending")
        tasks.close_suggestion_windows()
        self.assertEqual(_outcome(s).status, "not_bought")

    def test_first_qualifying_purchase_wins_whatever_the_ingest_order(self):
        s = _suggest(self.c["bon10"], self.jane, shown_at=self.t0)
        exact = _tx(self.t0 + timedelta(days=3), "P-BON10", "EX")
        sibling = _tx(self.t0 + timedelta(days=1), "P-BON20", "SIB")
        self._sync([exact])
        self.assertEqual(_outcome(s).status, "bought_exact")
        # A full rebuild also sees the EARLIER sibling purchase: that one was first, so it wins.
        self._sync([exact, sibling], rebuild=True)
        o = _outcome(s)
        self.assertEqual((o.status, o.match_kind, o.matched_line), ("bought_sibling", "sibling_size", "SIB:P-BON20:0"))

    def test_re_ingesting_the_same_transactions_is_idempotent(self):
        s = _suggest(self.c["bon10"], self.jane, shown_at=self.t0)
        txs = [_tx(self.t0 + timedelta(days=1), "P-BON10", "T1"), _tx(self.t0 + timedelta(days=2), "P-BON20", "T2")]
        self._sync(txs)
        first = SuggestionOutcome.objects.filter(suggestion=s).values().get()
        self._sync(txs)                 # same pull again (watermark-gated)
        self._sync(txs, rebuild=True)   # full refold of the same lines
        self._sync(txs, rebuild=True)
        self.assertEqual(SuggestionOutcome.objects.filter(suggestion=s).values().get(), first)
        self.assertEqual(first["status"], "bought_exact")
        lines = [{"bought_at": (self.t0 + timedelta(days=1)).isoformat(), "product_id": "P-BON10", "sku": "BON10",
                  "tx_line": "T1:P-BON10:0", "line_total": 10}]
        self.assertEqual(suggestions.attribute_lines(self.jane, lines), 0)

    def test_an_anonymous_session_linked_later_is_attributed(self):
        sess = ChatSession.objects.create(session_token=TOKEN, channel="chat")
        s = _suggest(self.c["bon10"], None, shown_at=self.t0, session=sess)
        self._sync([_tx(self.t0 + timedelta(days=1), "P-BON10", "T1")])  # bought while still anonymous
        self.assertEqual(_outcome(s).status, "pending")
        identity.link_session(TOKEN, self.jane, PHONE, "web_phone")
        s.refresh_from_db()
        self.assertEqual((s.customer_id, s.identity_via), (self.jane.pk, "web_phone"))
        # The purchase was already in purchase_history and its timestamp is inside the window: certain.
        self.assertEqual((_outcome(s).status, _outcome(s).match_kind), ("bought_exact", "exact"))

    def test_a_linked_session_converts_on_a_later_purchase(self):
        sess = ChatSession.objects.create(session_token=TOKEN, channel="questionnaire")
        s = _suggest(self.c["blush_sk"], None, shown_at=self.t0, session=sess)
        identity.link_session(TOKEN, self.jane, PHONE, "web_phone")
        self.assertEqual(_outcome(s).status, "pending")
        self._sync([_tx(self.t0 + timedelta(days=2), "P-BLWM", "T9")])
        self.assertEqual(_outcome(s).match_kind, "sibling_strain")

    def test_a_session_that_changed_hands_keeps_the_first_persons_suggestions(self):
        bob = CustomerProfile.objects.create(phone="+15095550199", name="Bob")
        sess = ChatSession.objects.create(session_token=TOKEN, channel="chat", customer=bob, identity_via="web_phone")
        s = _suggest(self.c["bon10"], bob, shown_at=self.t0, session=sess)
        identity.link_session(TOKEN, self.jane, PHONE, "web_phone")
        s.refresh_from_db()
        self.assertEqual(s.customer_id, bob.pk)

    def test_never_linked_is_unattributable_and_the_close_honours_the_grace_day(self):
        now = dj_tz.now()
        anon = _suggest(self.c["bon10"], None, shown_at=now - timedelta(days=12))
        known = _suggest(self.c["bon20"], self.jane, shown_at=now - timedelta(days=12))
        in_grace = _suggest(self.c["bd35"], self.jane, shown_at=now - timedelta(days=10, hours=12))
        open_ = _suggest(self.c["wc28"], self.jane, shown_at=now - timedelta(days=3))
        self.assertEqual(tasks.close_suggestion_windows(), {"not_bought": 1, "unattributable": 1})
        self.assertEqual(_outcome(anon).status, "unattributable")
        self.assertEqual(_outcome(known).status, "not_bought")
        self.assertEqual(_outcome(in_grace).status, "pending")   # window ended 12h ago: sync lag grace
        self.assertEqual(_outcome(open_).status, "pending")
        self.assertEqual(tasks.close_suggestion_windows(), {"not_bought": 0, "unattributable": 0})  # idempotent

    def test_the_close_job_is_on_the_hourly_beat(self):
        from core.celery import app

        entry = app.conf.beat_schedule["close-suggestion-windows-hourly"]
        self.assertEqual((entry["task"], entry["schedule"]), ("budtender.tasks.close_suggestion_windows", 3600.0))


# ── backfill + on-demand evaluation ──────────────────────────────────────────
@override_settings(CACHES=_LOCMEM)
class BackfillTests(TestCase):
    def setUp(self):
        self.c = _catalog()
        now = dj_tz.now()
        self.now = now
        self.jane = CustomerProfile.objects.create(phone=PHONE, name="Jane Doe", purchase_history=[
            {"product_id": "P-BON10", "sku": "BON10", "product_name": self.c["bon10"].name, "brand": "Verdelux",
             "category": "edibles", "times_bought": 1, "last_price": 18.0,
             "first_bought_at": (now - timedelta(days=28)).isoformat(),
             "last_bought_at": (now - timedelta(days=28)).isoformat()},
            # bought before AND after the window only: no certainty either way
            {"product_id": "P-BD35", "sku": "BD35", "product_name": self.c["bd35"].name, "brand": "Phat Panda",
             "category": "flower", "strain": "Blue Dream", "times_bought": 3,
             "first_bought_at": (now - timedelta(days=60)).isoformat(),
             "last_bought_at": (now - timedelta(days=2)).isoformat()},
        ])
        sess = ChatSession.objects.create(session_token="vc-old-call", channel="voice", identity_via="caller_id",
                                          customer=self.jane)

        def legacy(sku, days_ago, customer=None, session=None, source="chat", kind="primary"):
            sp = SuggestedProduct.objects.create(session=session, customer=customer, location_slug="yakima", sku=sku,
                                                 source=source, kind=kind)
            SuggestedProduct.objects.filter(pk=sp.pk).update(shown_at=now - timedelta(days=days_ago))
            return sp

        self.proven = legacy("BON10", 30, self.jane, sess, source="voice")   # bought 2 days after: certain
        self.masked = legacy("BD35", 40, self.jane)                        # in-window buy can't be proven
        self.gone = legacy("RETIRED-SKU", 40)
        self.recent = legacy("BLSK", 3, self.jane, source="questionnaire")
        self.pairing = legacy("WC28", 5, None, kind="pairing", source="menu")

    def test_dry_run_writes_nothing_then_apply_never_guesses(self):
        call_command("backfill_suggestion_snapshots")
        self.assertEqual(SuggestionOutcome.objects.count(), 0)
        self.assertEqual(SuggestedProduct.objects.filter(snapshot={}).count(), 5)

        stats = suggestions.backfill(apply=True, now=self.now)
        self.assertEqual((stats["rows"], stats["partial"], stats["outcomes_created"]), (5, 1, 5))
        p = SuggestedProduct.objects.get(pk=self.proven.pk)
        self.assertEqual((p.snapshot["name"], p.snapshot["brand"], p.snapshot["backfilled"]),
                         (self.c["bon10"].name, "Verdelux", True))
        self.assertEqual((p.channel, p.identity_via), ("phone", "caller_id"))
        self.assertEqual((_outcome(p).status, _outcome(p).match_kind), ("bought_exact", "exact"))
        self.assertEqual(_outcome(self.masked).status, "unattributable")       # never "not_bought" by guess
        gone = SuggestedProduct.objects.get(pk=self.gone.pk)
        self.assertTrue(gone.snapshot["snapshot_partial"])
        self.assertEqual((gone.sibling_key, _outcome(gone).status), ("", "unattributable"))
        self.assertEqual(_outcome(self.recent).status, "pending")              # window still open
        self.assertEqual(SuggestedProduct.objects.get(pk=self.recent.pk).channel, "questionnaire")
        self.assertEqual(SuggestedProduct.objects.get(pk=self.pairing.pk).channel, "pairing")
        self.assertNotIn("cost", json.dumps(list(SuggestedProduct.objects.values_list("snapshot", flat=True))))
        again = suggestions.backfill(apply=True, now=self.now)
        self.assertEqual((again["rows"], again["outcomes_created"]), (0, 0))

    def test_evaluate_command_dry_run_then_apply(self):
        suggestions.backfill(apply=True, now=self.now)
        # Jane's history now proves she bought the Blush line (other flavour) after the recent suggestion.
        self.jane.purchase_history.append({
            "product_id": "P-BLWM", "sku": "BLWM", "product_name": self.c["blush_wm"].name, "brand": "Blush",
            "category": "edibles", "first_bought_at": (self.now - timedelta(days=1)).isoformat(),
            "last_bought_at": (self.now - timedelta(days=1)).isoformat()})
        self.jane.save()
        call_command("evaluate_suggestions", "--days", "60")
        self.assertEqual(_outcome(self.recent).status, "pending")
        call_command("evaluate_suggestions", "--days", "60", "--apply")
        self.assertEqual((_outcome(self.recent).status, _outcome(self.recent).match_kind),
                         ("bought_sibling", "sibling_strain"))


# ── API ──────────────────────────────────────────────────────────────────────
@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class ApiTests(TestCase):
    """Hand-computed fixture (days=30): 7 rows in the window, 1 older.

    #  channel        kind     store    customer  product  status
    1  chat           primary  yakima   C1        A        bought_exact
    2  chat           primary  yakima   C1        B        not_bought
    3  phone          primary  yakima   C2        A        bought_sibling (sibling_size)
    4  phone          primary  pullman  C2        C        not_bought
    5  questionnaire  primary  yakima   -         A        unattributable
    6  questionnaire  primary  yakima   -         B        pending
    7  pairing        pairing  pullman  C1        C        not_bought
    8  chat (40 days ago)                C1        A        bought_exact   <- outside days=30
    conversion = bought_any / (bought_any + not_bought) = 2 / (2 + 3) = 0.4
    """

    def setUp(self):
        c = _catalog()
        self.A, self.B = c["bon10"], c["bd35"]
        self.C = _product("PC1", "Wyld Gummies Strawberry 10pk", "Wyld", "edibles", pid="P-WYLD", loc="pullman")
        self.c1 = CustomerProfile.objects.create(phone=PHONE, name="Jane Doe")
        self.c2 = CustomerProfile.objects.create(phone="+15095550188", name="Sam Roe")
        now = dj_tz.now()
        spec = [("chat", "primary", self.c1, self.A, "bought_exact", "exact", 1),
                ("chat", "primary", self.c1, self.B, "not_bought", "", 2),
                ("phone", "primary", self.c2, self.A, "bought_sibling", "sibling_size", 3),
                ("phone", "primary", self.c2, self.C, "not_bought", "", 4),
                ("questionnaire", "primary", None, self.A, "unattributable", "", 5),
                ("questionnaire", "primary", None, self.B, "pending", "", 6),
                ("pairing", "pairing", self.c1, self.C, "not_bought", "", 7),
                ("chat", "primary", self.c1, self.A, "bought_exact", "exact", 40)]
        self.rows = []
        for ch, kind, cust, p, status, mk, ago in spec:
            sess = ChatSession.objects.create(session_token=f"s-fixture-session-{len(self.rows):02d}", channel="chat",
                                              customer=cust, identity_via="web_phone" if cust else "")
            r = _suggest(p, cust, shown_at=now - timedelta(days=ago), session=sess, channel=ch, kind=kind)
            fields = {"status": status, "match_kind": mk}
            if status.startswith("bought"):
                fields.update(matched_at=now - timedelta(days=ago) + timedelta(days=2), matched_sku="BON20"
                              if mk == "sibling_size" else p.sku, matched_name="Bought it", matched_amount=32)
            SuggestionOutcome.objects.filter(suggestion=r).update(**fields)
            self.rows.append(r)

    def test_summary_numbers(self):
        body = _post("/api/v1/analytics/suggestions", {"days": 30}).json()
        t = body["totals"]
        self.assertEqual(body["window_days"], 30)
        self.assertEqual({k: t[k] for k in ("suggested", "products", "customers_known", "pending", "bought_exact",
                                            "bought_sibling", "bought_any", "not_bought", "unattributable")},
                         {"suggested": 7, "products": 3, "customers_known": 2, "pending": 1, "bought_exact": 1,
                          "bought_sibling": 1, "bought_any": 2, "not_bought": 3, "unattributable": 1})
        self.assertEqual((t["conversion_rate"], t["exact_rate"], t["sibling_rate"]), (0.4, 0.2, 0.2))
        ch = {g["key"]: g for g in body["by_channel"]}
        self.assertEqual({k: (g["suggested"], g["conversion_rate"]) for k, g in ch.items()},
                         {"chat": (2, 0.5), "phone": (2, 0.5), "questionnaire": (2, None), "pairing": (1, 0.0)})
        self.assertEqual({g["key"]: g["suggested"] for g in body["by_store"]}, {"yakima": 5, "pullman": 2})
        self.assertEqual({g["key"]: (g["suggested"], g["conversion_rate"]) for g in body["by_kind"]},
                         {"primary": (6, 0.5), "pairing": (1, 0.0)})
        self.assertEqual([(g["key"], g["label"], g["suggested"]) for g in body["by_rank"]], [(1, "#1", 7)])
        self.assertEqual(sum(g["suggested"] for g in body["by_day"]), 7)
        self.assertEqual({g["key"]: g["suggested"] for g in body["by_category"]}, {"edibles": 5, "flower": 2})
        top = body["top_products"]
        self.assertEqual([(p["name"], p["times_suggested"], p["customers"], p["bought_exact"], p["bought_sibling"],
                           p["not_bought"], p["conversion_rate"]) for p in top],
                         [(self.A.name, 3, 2, 1, 1, 0, 1.0), (self.B.name, 2, 1, 0, 0, 1, 0.0),
                          (self.C.name, 2, 2, 0, 0, 2, 0.0)])
        a = top[0]
        self.assertEqual((a["brand"], a["category"], a["price"], a["thc_percent"], a["key"]),
                         ("Verdelux", "edibles", 18.0, 22.5, "pid:P-BON10"))
        self.assertEqual(top[1]["strain"], "Blue Dream")
        self.assertEqual(top[1]["size_label"], "3.5g")
        self.assertEqual([p["name"] for p in body["never_bought"]], [self.C.name, self.B.name])
        self.assertEqual({r["id"] for r in body["recent_buyers"]}, {self.rows[0].pk, self.rows[2].pk})
        blob = json.dumps(body).lower()
        for word in ("cost", "margin", PHONE, "+15095550188", "s-fixture"):
            self.assertNotIn(word.lower(), blob)

    def test_summary_filters_and_bounds(self):
        self.assertEqual(_post("/api/v1/analytics/suggestions", {"days": 30, "channel": "phone"}).json()["totals"]
                         ["suggested"], 2)
        self.assertEqual(_post("/api/v1/analytics/suggestions", {"days": 30, "kind": "pairing"}).json()["totals"]
                         ["suggested"], 1)
        self.assertEqual(_post("/api/v1/analytics/suggestions", {"days": 30, "store": "pullman"}).json()["totals"]
                         ["suggested"], 2)
        self.assertEqual(_post("/api/v1/analytics/suggestions", {"days": 30, "brand": "verdelux"}).json()["totals"]
                         ["suggested"], 3)
        self.assertEqual(_post("/api/v1/analytics/suggestions", {"days": 30, "category": "flower"}).json()["totals"]
                         ["suggested"], 2)
        wide = _post("/api/v1/analytics/suggestions", {"days": 99999}).json()
        self.assertEqual((wide["window_days"], wide["totals"]["suggested"], wide["totals"]["conversion_rate"]),
                         (365, 8, 0.5))
        junk = _post("/api/v1/analytics/suggestions", {"days": "x", "channel": "'; drop", "store": "nowhere"}).json()
        self.assertEqual((junk["window_days"], junk["totals"]["suggested"]), (30, 7))

    def test_list_shape_filters_paging_and_sort(self):
        body = _post("/api/v1/analytics/suggestions/list", {"days": 30, "limit": 2, "offset": 0}).json()
        self.assertEqual((body["total"], body["count"], body["sort"]), (7, 2, "-shown_at"))
        self.assertEqual([r["id"] for r in body["rows"]], [self.rows[0].pk, self.rows[1].pk])
        r = body["rows"][0]
        self.assertEqual(set(r), {"id", "suggested_at", "channel", "store", "kind", "sku", "customer", "identity_via",
                                  "snapshot", "status", "match_kind", "matched_name", "matched_sku", "matched_amount",
                                  "matched_at", "window_ends_at", "days_to_purchase", "session"})
        self.assertEqual(r["customer"], {"id": self.c1.pk, "name": "Jane Doe"})
        self.assertEqual((r["status"], r["days_to_purchase"], r["matched_amount"]), ("bought_exact", 2.0, 32.0))
        self.assertEqual(r["snapshot"]["name"], self.A.name)
        self.assertEqual(r["session"], {"id": self.rows[0].session_id})
        page2 = _post("/api/v1/analytics/suggestions/list", {"days": 30, "limit": 2, "offset": 6}).json()
        self.assertEqual([x["id"] for x in page2["rows"]], [self.rows[6].pk])
        nb = _post("/api/v1/analytics/suggestions/list", {"days": 30, "status": "not_bought"}).json()
        self.assertEqual(nb["total"], 3)
        sib = _post("/api/v1/analytics/suggestions/list", {"days": 30, "match_kind": "sibling_size"}).json()
        self.assertEqual([x["matched_sku"] for x in sib["rows"]], ["BON20"])
        asc = _post("/api/v1/analytics/suggestions/list", {"days": 30, "sort": "shown_at", "limit": 1}).json()
        self.assertEqual(asc["rows"][0]["id"], self.rows[6].pk)
        bad = _post("/api/v1/analytics/suggestions/list", {"days": 30, "sort": "snapshot; drop", "limit": 500}).json()
        self.assertEqual((bad["sort"], bad["limit"]), ("-shown_at", 100))
        blob = json.dumps(_post("/api/v1/analytics/suggestions/list", {"days": 365, "limit": 100}).json())
        self.assertNotIn(PHONE, blob)
        self.assertNotIn("s-fixture", blob)

    def test_two_customers_never_see_each_others_rows(self):
        one = _post("/api/v1/customer/suggestions", {"id": self.c1.pk}).json()
        two = _post("/api/v1/customer/suggestions", {"id": self.c2.pk}).json()
        self.assertEqual({r["id"] for r in one["rows"]}, {self.rows[i].pk for i in (0, 1, 6, 7)})
        self.assertEqual({r["id"] for r in two["rows"]}, {self.rows[2].pk, self.rows[3].pk})
        self.assertEqual({r["customer"]["id"] for r in one["rows"]}, {self.c1.pk})
        self.assertEqual(one["customer"], {"id": self.c1.pk, "name": "Jane Doe"})
        self.assertEqual((one["totals"]["bought_exact"], one["totals"]["not_bought"], one["totals"]["conversion_rate"]),
                         (2, 2, 0.5))
        self.assertNotIn(PHONE, json.dumps(one))
        self.assertEqual(_post("/api/v1/customer/suggestions", {"id": 999999}).status_code, 404)
        self.assertEqual(_post("/api/v1/customer/suggestions", {}).status_code, 400)
        mine = _post("/api/v1/analytics/suggestions/list", {"days": 30, "customer_id": self.c2.pk}).json()
        self.assertEqual(mine["total"], 2)

    def test_chat_history_by_customer(self):
        body = _post("/api/v1/chat/history", {"customer_id": self.c2.pk}).json()
        self.assertEqual(body["total"], 2)
        self.assertEqual({r["customer_id"] for r in body["sessions"]}, {self.c2.pk})
        self.assertEqual({r["identity_via"] for r in body["sessions"]}, {"web_phone"})
        self.assertNotIn("session_token", json.dumps(body))
        # an {id} read narrowed to a customer never returns someone else's transcript
        other = self.rows[0].session_id
        self.assertEqual(_post("/api/v1/chat/history", {"id": other, "customer_id": self.c2.pk}).json()["sessions"], [])
        self.assertEqual(_post("/api/v1/chat/history", {}).json()["total"], ChatSession.objects.count())

    def test_the_new_endpoints_are_staff_only(self):
        for path in ("/api/v1/analytics/suggestions", "/api/v1/analytics/suggestions/list",
                     "/api/v1/customer/suggestions"):
            self.assertIn(_post(path, {"id": self.c1.pk}, WEBSITE).status_code, (401, 403), path)
            self.assertIn(_post(path, {}, "nope").status_code, (401, 403), path)


def test_snapshot_datetime_helper_reads_naive_and_zulu():
    assert suggestions._aware("2026-01-05T10:00:00Z") == datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
    assert suggestions._aware("2026-01-05T10:00:00") == datetime(2026, 1, 5, 10, tzinfo=timezone.utc)
    assert suggestions._aware("not a date") is None
