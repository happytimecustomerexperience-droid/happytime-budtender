"""Two people are never one session, and one person's session never shows another's data.

Each test sets up TWO visitors/customers and pins that what belongs to one (messages, the phone
link, taste-first picks, suggestion rows, a resumed transcript) never reaches the other: a shared
screen, two shoppers behind the website's single proxy address, a number typed by someone else, a
stale or handed-on token, a degenerate client token.
"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest
from django.test import Client, TestCase, override_settings

from budtender import views
from budtender.models import (AnalyticsEvent, ChatMessage, ChatSession, CustomerProfile, Product,
                              SuggestedProduct)

BACKEND, WEBSITE = "backend-token", "website-token"
ALICE, BOB = "+15095550111", "+15095550122"
TOKEN_A = "s-" + "A1b2C3d4E5" * 3   # 30 chars after "s-": minted shape
TOKEN_B = "s-" + "Z9y8X7w6V5" * 3
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    monkeypatch.setattr("budtender.views.fire", lambda *a, **k: False)


def _post(path, payload, token=BACKEND, **headers):
    return Client().post(path, data=json.dumps(payload), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}", **headers)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class GuessableTokenTests(TestCase):
    """A short client token ("s-1", "s-undefined", a dev id) is a string two people can both send."""

    def test_two_visitors_sending_the_same_degenerate_token_never_share_a_conversation(self):
        with patch("budtender.views.generate_chat_reply_with_source", return_value=("hi", "brain", "")):
            a = _post("/api/v1/chat/message", {"session_token": "s-undefined", "message": "alice private"}, WEBSITE)
            b = _post("/api/v1/chat/message", {"session_token": "s-undefined", "message": "bob private"}, WEBSITE)
        ta, tb = a.json()["session_token"], b.json()["session_token"]
        self.assertNotEqual(ta, tb)
        self.assertNotIn("s-undefined", (ta, tb))
        self.assertFalse(ChatSession.objects.filter(session_token="s-undefined").exists())
        self.assertEqual(list(ChatMessage.objects.filter(session__session_token=tb, role="user")
                              .values_list("content", flat=True)), ["bob private"])

    def test_persist_search_and_identify_never_create_a_session_under_a_short_token(self):
        CustomerProfile.objects.create(phone=ALICE, name="Alice", total_orders=2)
        _post("/api/v1/chat/persist/", {"session_token": "s-1", "messages": [{"role": "user", "content": "x"}]},
              WEBSITE)
        _post("/api/v1/customer/session-context", {"session_token": "s-null", "phone": ALICE}, WEBSITE)
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products", return_value=[]):
            _post("/api/v1/products/search/", {"slots": {"store": "yakima"}, "session_token": "s-test"}, WEBSITE)
        self.assertFalse(ChatSession.objects.filter(session_token__in=["s-1", "s-null", "s-test"]).exists())

    def test_an_overlong_minted_looking_token_is_replaced_not_stored(self):
        with patch("budtender.views.generate_chat_reply_with_source", return_value=("hi", "brain", "")):
            r = _post("/api/v1/chat/message", {"session_token": "s-" + "x" * 80, "message": "hi"}, WEBSITE)
        self.assertLessEqual(len(r.json()["session_token"]), 64)
        self.assertNotEqual(r.json()["session_token"], "s-" + "x" * 80)

    def test_a_website_shaped_token_still_works(self):
        tok = "s-mg3k1z9a-k3j4h5g6"  # lib/chat/persist.ts generateSessionId()
        _post("/api/v1/chat/persist/", {"session_token": tok, "messages": [{"role": "user", "content": "x"}]},
              WEBSITE)
        self.assertTrue(ChatSession.objects.filter(session_token=tok).exists())


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class ResumeByPhoneTests(TestCase):
    URL = "/api/v1/chat/resume-by-phone"

    def setUp(self):
        self.alice = CustomerProfile.objects.create(phone=ALICE, name="Alice", total_orders=3)

    def test_a_stranger_who_typed_my_number_on_the_website_is_never_resumed_to_me(self):
        # Bob typed Alice's number on the website: his session is web_phone-linked to her row.
        bob_web = ChatSession.objects.create(session_token=TOKEN_B, phone=ALICE, customer=self.alice,
                                             identity_via="web_phone", slots={"budget": "bob's"})
        ChatMessage.objects.create(session=bob_web, role="user", content="bob's private question")
        SuggestedProduct.objects.create(session=bob_web, location_slug="yakima", sku="BOB-SKU")

        body = _post(self.URL, {"phone": ALICE, "current_session_token": "vc-call-1"}).json()

        self.assertFalse(body["resumed"])
        self.assertEqual(body["messages"], [])
        self.assertNotEqual(body["session_token"], TOKEN_B)
        self.assertNotIn("bob", json.dumps(body))

    def test_my_own_caller_id_conversation_still_resumes(self):
        mine = ChatSession.objects.create(session_token="vc-earlier", phone=ALICE, customer=self.alice,
                                          identity_via="caller_id")
        ChatMessage.objects.create(session=mine, role="user", content="alice asked this")
        body = _post(self.URL, {"phone": ALICE, "current_session_token": "vc-now"}).json()
        self.assertTrue(body["resumed"])
        self.assertEqual(body["session_token"], "vc-earlier")

    def test_a_shared_number_resumes_nobodys_chat(self):
        shared = CustomerProfile.objects.create(phone=BOB, dutchie_ids=["1", "2", "3", "4"], total_orders=9)
        other = ChatSession.objects.create(session_token="vc-family", phone=BOB, customer=shared,
                                           identity_via="caller_id")
        ChatMessage.objects.create(session=other, role="user", content="another family member")
        body = _post(self.URL, {"phone": BOB, "current_session_token": "vc-now"}).json()
        self.assertFalse(body["resumed"])
        self.assertNotIn("family member", json.dumps(body))


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM,
                   HHT_WEB_PHONE_IDENTITY=True)
class SharedScreenTests(TestCase):
    """One tab, two shoppers: the second must not keep the first one's identification."""

    CTX = "/api/v1/customer/session-context"

    def setUp(self):
        self.alice = CustomerProfile.objects.create(phone=ALICE, name="Alice", total_orders=3)
        _post(self.CTX, {"session_token": TOKEN_A, "phone": ALICE}, WEBSITE)
        self.assertEqual(ChatSession.objects.get(session_token=TOKEN_A).customer, self.alice)

    def _search_profile(self):
        seen = []
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products", side_effect=lambda loc, s, p, **kw: seen.append(p) or []):
            _post("/api/v1/products/search/", {"slots": {"store": "yakima"}, "session_token": TOKEN_A}, WEBSITE)
        return seen[0]

    def _assert_unlinked(self):
        s = ChatSession.objects.get(session_token=TOKEN_A)
        self.assertEqual((s.customer, s.phone, s.identity_via), (None, "", ""))
        self.assertIsNone(self._search_profile())

    def test_the_next_shopper_typing_a_junk_number_drops_the_previous_link(self):
        body = _post(self.CTX, {"session_token": TOKEN_A, "phone": "0000000000"}, WEBSITE).json()
        self.assertEqual(body["first_name"], "")
        self._assert_unlinked()

    def test_clearing_the_number_or_skipping_drops_the_previous_link(self):
        _post(self.CTX, {"session_token": TOKEN_A, "forget": True}, WEBSITE)
        self._assert_unlinked()

    def test_a_shared_family_number_drops_the_previous_link(self):
        CustomerProfile.objects.create(phone=BOB, dutchie_ids=["1", "2", "3", "4"], total_orders=9)
        _post(self.CTX, {"session_token": TOKEN_A, "phone": BOB}, WEBSITE)
        self._assert_unlinked()

    def test_another_sessions_link_is_untouched(self):
        _post(self.CTX, {"session_token": TOKEN_B, "phone": ALICE}, WEBSITE)
        _post(self.CTX, {"session_token": TOKEN_B, "forget": True}, WEBSITE)
        self.assertEqual(ChatSession.objects.get(session_token=TOKEN_A).customer, self.alice)
        self.assertEqual(self._search_profile(), self.alice)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM,
                   HHT_WEB_PHONE_IDENTITY=True)
class OneShopperCannotSpendEveryonesBudgetTests(TestCase):
    CTX = "/api/v1/customer/session-context"

    def test_one_visitor_ip_rotating_sessions_is_capped_and_others_still_identify(self):
        codes = []
        for i in range(views.SESSION_CONTEXT_PER_IP_HOUR + 5):
            tok = f"s-attackerrr{i:04d}"
            codes.append(_post(self.CTX, {"session_token": tok, "phone": f"+1509555{i:04d}"}, WEBSITE,
                               HTTP_X_HHT_CLIENT_IP="203.0.113.7").status_code)
        self.assertEqual(codes.count(200), views.SESSION_CONTEXT_PER_IP_HOUR)
        self.assertEqual(codes[-1], 429)
        ok = _post(self.CTX, {"session_token": TOKEN_B, "phone": BOB}, WEBSITE, HTTP_X_HHT_CLIENT_IP="198.51.100.9")
        self.assertEqual(ok.status_code, 200)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class CallerIdBeatsAStaleTokenTests(TestCase):
    """A token tied to Alice arrives with Bob's caller-ID: Bob's picks are ranked for Bob and are never
    written into Alice's session."""

    def setUp(self):
        self.alice = CustomerProfile.objects.create(phone=ALICE, name="Alice", total_orders=3)
        self.bob = CustomerProfile.objects.create(phone=BOB, name="Bob", total_orders=2)
        self.session = ChatSession.objects.create(session_token=TOKEN_A, phone=ALICE, customer=self.alice,
                                                  identity_via="caller_id", location_slug="yakima")
        self.product = Product.objects.create(sku="P1", location_slug="yakima", name="Pick", price=10,
                                              quantity_on_hand=50)

    def test_search(self):
        seen = []
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products",
                   side_effect=lambda loc, s, p, **kw: seen.append(p) or [(self.product, "x")]):
            r = _post("/api/v1/products/search/",
                      {"slots": {"store": "yakima"}, "session_token": TOKEN_A, "phone": BOB})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(seen[0], self.bob)
        self.assertFalse(SuggestedProduct.objects.filter(session=self.session).exists())
        self.assertEqual(ChatSession.objects.get(pk=self.session.pk).customer, self.alice)

    def test_the_same_person_still_records_into_their_session(self):
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products", return_value=[(self.product, "x")]):
            _post("/api/v1/products/search/", {"slots": {"store": "yakima"}, "session_token": TOKEN_A, "phone": ALICE})
        self.assertEqual(SuggestedProduct.objects.get(session=self.session).customer, self.alice)

    def test_pairing(self):
        with patch("budtender.views.pair_for", return_value=(self.product, "complement", "goes well", 0.5)):
            _post("/api/v1/pairing/for-sku", {"location": "yakima", "sku": "P1", "session_token": TOKEN_A,
                                              "phone": BOB})
        row = SuggestedProduct.objects.get(kind="pairing")
        self.assertEqual((row.session, row.customer), (None, self.bob))


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class PhoneHashTests(TestCase):
    def test_an_analytics_phone_hash_is_keyed_not_a_reversible_bare_sha256(self):
        _post("/api/v1/track/", {"event_type": "chat_open", "session_token": TOKEN_A, "phone": ALICE}, WEBSITE)
        stored = AnalyticsEvent.objects.get(session_token=TOKEN_A).phone_hash
        self.assertTrue(stored)
        self.assertNotEqual(stored, hashlib.sha256(ALICE.encode()).hexdigest())
        self.assertEqual(stored, views._hash_phone("(509) 555-0111"))  # still stable per number
        self.assertNotEqual(stored, views._hash_phone(BOB))
