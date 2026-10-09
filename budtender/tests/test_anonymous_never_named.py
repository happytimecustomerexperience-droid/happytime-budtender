"""Regression: nobody is greeted by name unless a real, single person's number was given.

Owner report: the Mount Vernon and Pullman chat said "Hey Jaime" to a visitor who was not logged in and
had entered no phone. "Jaime" is in no source file, so it is data: a profile row. These pin every
backend way such a row could reach a visitor: a blank/short/junk number, a store's own line, a
placeholder, a number that is really a shared walk-in account (several Dutchie customer ids folded into
one phone), a stale session link, and a name another visitor typed onto a never-purchased row.
"""
from __future__ import annotations

import json

import pytest
from django.test import Client, TestCase, override_settings

from budtender import identity
from budtender.models import ChatSession, CustomerProfile

BACKEND, WEBSITE = "backend-token", "website-token"
TOKEN = "s-" + "j" * 24
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Stores' own lines (bundles.catalog.STORES) and other numbers that name nobody.
STORE_LINES = ["(360) 488-2923", "(509) 334-2788", "(509) 571-1106"]
JUNK = ["0000000000", "1111111111", "5555555555", "1234567890", "0955551234", "5090551234"]
BLANKISH = ["", None, "   ", "0", "+1", "555-1234", "12345", "(509) 555-12", "x" * 30]


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    monkeypatch.setattr("budtender.views.fire", lambda *a, **k: False)


def _post(path, payload, token=WEBSITE):
    return Client().post(path, data=json.dumps(payload), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


def _jaime(phone, **kw):
    return CustomerProfile.objects.create(phone=phone, name="Jaime Lopez", total_orders=40, **kw)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class AnonymousIsNeverNamedTests(TestCase):
    def test_a_blank_or_unusable_phone_resolves_nobody_even_if_a_blank_phone_row_exists(self):
        _jaime("")  # the corrupt-data shape: a profile whose phone is empty
        for raw in BLANKISH:
            with self.subTest(raw=raw):
                self.assertIsNone(identity.profile_for_phone(raw))
                self.assertEqual(identity.ensure_profile(raw, "web", "Jaime"), (None, False))
                self.assertEqual(identity.context(identity.profile_for_phone(raw))["first_name"], "")

    def test_a_store_line_or_placeholder_never_resolves_a_name(self):
        for raw in STORE_LINES + JUNK:
            e164 = "+1" + "".join(c for c in raw if c.isdigit())[-10:]
            _jaime(e164)
            with self.subTest(raw=raw):
                self.assertTrue(identity.non_identifying_phone(raw))
                self.assertIsNone(identity.profile_for_phone(raw))
                self.assertEqual(identity.ensure_profile(raw, "web", "Jaime"), (None, False))

    def test_an_ordinary_number_still_resolves(self):
        _jaime("+15095550142")
        self.assertFalse(identity.non_identifying_phone("(509) 555-0142"))
        self.assertEqual(identity.profile_for_phone("(509) 555-0142").name, "Jaime Lopez")

    def test_the_website_endpoint_stays_anonymous_and_links_nothing(self):
        _jaime("")
        for raw in BLANKISH + STORE_LINES + JUNK:
            e164 = "+1" + "".join(c for c in str(raw or "") if c.isdigit())[-10:]
            if len(e164) == 12:
                CustomerProfile.objects.get_or_create(phone=e164, defaults={"name": "Jaime Lopez", "total_orders": 40})
            with self.subTest(raw=raw):
                body = _post("/api/v1/customer/session-context", {"session_token": TOKEN, "phone": raw}).json()
                self.assertEqual((body["known"], body["first_name"], body["top_categories"]), (False, "", []))
                self.assertFalse(ChatSession.objects.filter(session_token=TOKEN, customer__isnull=False).exists())

    def test_no_phone_at_all_is_anonymous(self):
        _jaime("+15095550142")
        body = _post("/api/v1/customer/session-context", {"session_token": TOKEN}).json()
        self.assertEqual((body["known"], body["first_name"]), (False, ""))
        self.assertEqual(_post("/api/v1/customer/caller-context", {}, BACKEND).json()["first_name"], "")

    def test_the_voice_endpoint_does_not_create_or_name_a_store_line_caller(self):
        before = CustomerProfile.objects.count()
        for raw in STORE_LINES + JUNK + [""]:
            body = _post("/api/v1/customer/caller-context", {"phone": raw}, BACKEND).json()
            self.assertEqual((body["known"], body["first_name"], body["created"]), (False, "", False), raw)
        self.assertEqual(CustomerProfile.objects.count(), before)

    def test_a_shared_walk_in_number_is_not_a_person(self):
        # Four Dutchie customers folded into one phone = a store typing its own number for walk-ins.
        _jaime("+15095550177", dutchie_ids=["1", "2", "3", "4"])
        self.assertIsNone(identity.profile_for_phone("+15095550177"))
        self.assertEqual(identity.ensure_profile("+15095550177", "web", "Jaime"), (None, False))
        for token, url in ((WEBSITE, "/api/v1/customer/session-context"), (BACKEND, "/api/v1/customer/caller-context")):
            body = _post(url, {"session_token": TOKEN, "phone": "+15095550177"}, token).json()
            self.assertEqual((body["known"], body["first_name"], body["orders"]), (False, "", 0), url)

    def test_one_person_with_a_couple_of_dutchie_ids_is_still_one_person(self):
        _jaime("+15095550178", dutchie_ids=["1", "2", "3"])
        body = _post("/api/v1/customer/session-context", {"session_token": TOKEN, "phone": "+15095550178"}).json()
        self.assertEqual(body["first_name"], "Jaime")

    def test_a_session_already_linked_to_a_shared_row_does_not_carry_the_name(self):
        row = _jaime("+15095550177", dutchie_ids=["1", "2", "3", "4"])
        self.assertIsNone(identity.trusted(row))
        self.assertEqual(identity.context(row)["first_name"], "")
        identity.link_session(TOKEN, row, row.phone, "web_phone")  # a no-op on a shared row
        ChatSession.objects.create(session_token=TOKEN)
        identity.link_session(TOKEN, row, row.phone, "web_phone")
        self.assertIsNone(ChatSession.objects.get(session_token=TOKEN).customer)

    def test_a_name_typed_onto_a_never_purchased_number_is_not_echoed_to_the_next_visitor(self):
        phone = "+15095550188"
        first = _post("/api/v1/customer/session-context",
                      {"session_token": TOKEN, "phone": phone, "name": "Jaime"}).json()
        self.assertEqual(first["first_name"], "Jaime")  # they just told us their own name
        other = "s-" + "k" * 24
        second = _post("/api/v1/customer/session-context", {"session_token": other, "phone": phone}).json()
        self.assertEqual((second["known"], second["first_name"]), (False, ""))
        third = _post("/api/v1/customer/session-context",
                      {"session_token": other, "phone": phone, "name": "Someone Else"}).json()
        self.assertEqual(third["first_name"], "")

    def test_a_purchaser_is_still_greeted_on_the_website(self):
        _jaime("+15095550142")
        body = _post("/api/v1/customer/session-context", {"session_token": TOKEN, "phone": "(509) 555-0142"}).json()
        self.assertEqual((body["known"], body["first_name"]), (True, "Jaime"))

    @override_settings(HHT_NON_IDENTIFYING_PHONES=["509-555-0142"])
    def test_the_owner_can_add_numbers_that_name_nobody(self):
        _jaime("+15095550142")
        self.assertIsNone(identity.profile_for_phone("+15095550142"))

    def test_resume_by_phone_will_not_read_a_stranger_chat_through_a_store_line(self):
        ChatSession.objects.create(session_token="s-" + "m" * 24, phone="+13604882923")
        body = _post("/api/v1/chat/resume-by-phone", {"phone": "(360) 488-2923", "current_session_token": TOKEN},
                     BACKEND).json()
        self.assertFalse(body["resumed"])


@override_settings(CACHES=_LOCMEM)
class DiagnoseGreetingTests(TestCase):
    def test_it_flags_the_rows_that_could_greet_a_stranger_and_prints_no_full_numbers(self):
        from io import StringIO

        from django.core.management import call_command

        _jaime("+13604882923")                                      # the Mount Vernon store line
        _jaime("+15095550177", dutchie_ids=["1", "2", "3", "4"])     # a shared walk-in number
        _jaime("+15095550142")                                      # one real person
        out = StringIO()
        call_command("diagnose_greeting", "--name", "Jaime", stdout=out)
        text = out.getvalue()
        self.assertIn("3 row(s) flagged", text)
        self.assertIn("non-identifying number", text)
        self.assertIn("shared: 4 Dutchie customer ids", text)
        self.assertNotIn("+13604882923", text)
        self.assertNotIn("5095550177", text)
