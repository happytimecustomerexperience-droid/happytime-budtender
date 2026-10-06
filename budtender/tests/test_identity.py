"""Who is this person: caller-ID context, new-profile creation (ours, never Dutchie's), the typed-phone
website identity (owner-approved, switchable), and the weekly merge that only trusts a Dutchie id."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from django.test import Client, TestCase, override_settings

from budtender import identity, tasks
from budtender.models import ChatSession, CustomerProfile, PhoneCartDraft, SuggestedProduct
from customers.models import Customer as ScanCustomer

BACKEND, WEBSITE = "backend-token", "website-token"
P_DUTCHIE, P_SHELL = "+15095550101", "+15095550102"
TOKEN = "s-" + "a" * 24
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@pytest.fixture(autouse=True)
def _no_broker(monkeypatch):
    # profile-upsert fires the affinity recompute; with no reachable broker the first publish blocks for
    # over a minute (it made one of these tests take 79 s), and a test must never reach a broker.
    monkeypatch.setattr("budtender.views.fire", lambda *a, **k: False)


def _post(path, payload, token=BACKEND):
    return Client().post(path, data=json.dumps(payload), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


class FirstNameTests(TestCase):
    def test_reads_a_first_name_or_nothing(self):
        ok = {"Jordan Smith": "Jordan", "JORDAN": "Jordan", "jean-luc picard": "Jean-luc",
              "José García": "José", "O'Neil": "O'Neil", "  Dana  ": "Dana"}
        for raw, want in ok.items():
            self.assertEqual(identity.first_name(raw), want, raw)
        for raw in ("", None, "Smith, Jordan", "420 Dispensary LLC", "J0rdan", "!!", "x" * 40):
            self.assertEqual(identity.first_name(raw), "", raw)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class CallerContextTests(TestCase):
    URL = "/api/v1/customer/caller-context"

    def test_a_new_caller_gets_a_profile_in_our_db_and_it_is_reused(self):
        first = _post(self.URL, {"phone": "(509) 555-0199"}).json()
        self.assertTrue(first["created"])
        self.assertFalse(first["known"])
        self.assertEqual(first["first_name"], "")
        row = CustomerProfile.objects.get(phone="+15095550199")
        self.assertEqual((row.source, row.dutchie_ids, row.name), ("voice", [], ""))
        again = _post(self.URL, {"phone": "+15095550199"}).json()
        self.assertFalse(again["created"])
        self.assertEqual(CustomerProfile.objects.count(), 1)

    def test_a_returning_customer_comes_back_with_a_first_name_and_taste_only(self):
        CustomerProfile.objects.create(
            phone=P_DUTCHIE, name="Jordan Smith", total_orders=14, price_tier="mid",
            category_affinity={"flower": 0.7, "edible": 0.3}, brand_affinity={"Acme": 1.0},
            flavor_affinity={"citrus": 1.0}, terpene_affinity={"limonene": 1.0},
            last_purchase_at=datetime.now(timezone.utc) - timedelta(days=12),
            purchase_history=[{"sku": "x", "last_price": 31.0}],
        )
        body = _post(self.URL, {"phone": P_DUTCHIE}).json()
        self.assertTrue(body["known"] and body["has_history"])
        self.assertEqual(body["first_name"], "Jordan")
        self.assertEqual((body["top_categories"], body["brands"], body["days_since_last"]),
                         (["flower", "edible"], ["Acme"], 12))
        self.assertNotIn("Smith", json.dumps(body))
        self.assertNotIn("purchase_history", body)
        self.assertNotIn(P_DUTCHIE, json.dumps(body))

    def test_a_junk_number_creates_nothing(self):
        body = _post(self.URL, {"phone": "555-1234"}).json()
        self.assertEqual((body["known"], body["created"]), (False, False))
        self.assertEqual(CustomerProfile.objects.count(), 0)

    def test_create_false_only_looks(self):
        _post(self.URL, {"phone": P_SHELL, "create": False})
        self.assertEqual(CustomerProfile.objects.count(), 0)

    def test_it_links_the_call_session_and_the_website_token_cannot_call_it(self):
        ChatSession.objects.create(session_token="vc-1")
        _post(self.URL, {"phone": P_SHELL, "session_token": "vc-1"})
        s = ChatSession.objects.get(session_token="vc-1")
        self.assertEqual((s.customer.phone, s.identity_via, s.phone), (P_SHELL, "caller_id", P_SHELL))
        self.assertIn(_post(self.URL, {"phone": P_SHELL}, WEBSITE).status_code, (401, 403))

    def test_a_name_is_stored_only_when_the_row_has_none(self):
        up = "/api/v1/customer/profile-upsert"
        self.assertEqual(_post(up, {"phone": P_SHELL, "name": "Dana Lee"}).json()["first_name"], "Dana")
        _post(up, {"phone": P_SHELL, "name": "Someone Else"})
        self.assertEqual(CustomerProfile.objects.get(phone=P_SHELL).name, "Dana")
        CustomerProfile.objects.create(phone=P_DUTCHIE, name="Jordan Smith", source="dutchie")
        _post(up, {"phone": P_DUTCHIE, "name": "Mallory"})
        self.assertEqual(CustomerProfile.objects.get(phone=P_DUTCHIE).name, "Jordan Smith")
        self.assertEqual(_post(up, {"phone": "nope"}).status_code, 400)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class WebSessionIdentityTests(TestCase):
    CTX = "/api/v1/customer/session-context"

    def setUp(self):
        self.jordan = CustomerProfile.objects.create(phone=P_DUTCHIE, name="Jordan Smith", total_orders=3)

    def _search_profile(self, token_in_body, phone_in_body=""):
        seen = []
        payload = {"slots": {"store": "yakima"}, "session_token": token_in_body, "phone": phone_in_body}
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products",
                   side_effect=lambda loc, slots, profile, **kw: seen.append(profile) or []):
            self.assertEqual(_post("/api/v1/products/search/", payload, WEBSITE).status_code, 200)
        return seen[0]

    def test_a_typed_phone_identifies_the_session_and_search_personalises_from_it(self):
        body = _post(self.CTX, {"session_token": TOKEN, "phone": P_DUTCHIE}, WEBSITE).json()
        self.assertEqual((body["known"], body["first_name"]), (True, "Jordan"))
        s = ChatSession.objects.get(session_token=TOKEN)
        self.assertEqual((s.customer, s.identity_via), (self.jordan, "web_phone"))
        self.assertEqual(self._search_profile(TOKEN), self.jordan)

    def test_a_phone_in_the_search_body_or_an_unidentified_session_stays_anonymous(self):
        self.assertIsNone(self._search_profile("s-" + "b" * 24, phone_in_body=P_DUTCHIE))
        ChatSession.objects.create(session_token=TOKEN, customer=self.jordan)  # linked, never identified
        self.assertIsNone(self._search_profile(TOKEN))

    def test_a_new_visitor_gets_a_web_profile_and_a_name_they_give(self):
        body = _post(self.CTX, {"session_token": TOKEN, "phone": P_SHELL, "name": "Dana"}, WEBSITE).json()
        self.assertEqual((body["known"], body["created"], body["first_name"]), (False, True, "Dana"))
        self.assertEqual(CustomerProfile.objects.get(phone=P_SHELL).source, "web")

    @override_settings(HHT_WEB_PHONE_IDENTITY=False)
    def test_the_switch_turns_it_all_off(self):
        body = _post(self.CTX, {"session_token": TOKEN, "phone": P_DUTCHIE}, WEBSITE).json()
        self.assertEqual((body["known"], body["first_name"]), (False, ""))
        self.assertFalse(ChatSession.objects.filter(session_token=TOKEN).exists())
        ChatSession.objects.create(session_token=TOKEN, customer=self.jordan, identity_via="web_phone")
        self.assertIsNone(self._search_profile(TOKEN))  # an old identification stops counting too
        self.assertEqual(CustomerProfile.objects.filter(source="web").count(), 0)

    def test_it_is_capped_per_session_on_new_numbers_and_needs_a_session(self):
        for i in range(6):  # six DIFFERENT numbers spend the cap...
            self.assertEqual(_post(self.CTX, {"session_token": TOKEN, "phone": f"+1509555020{i}"}, WEBSITE).status_code, 200)
        self.assertEqual(_post(self.CTX, {"session_token": TOKEN, "phone": P_DUTCHIE}, WEBSITE).status_code, 429)
        body = _post(self.CTX, {"phone": P_DUTCHIE}, WEBSITE).json()
        self.assertFalse(body["known"])

    def test_asking_again_for_the_linked_number_is_free_so_every_chat_turn_can_do_it(self):
        for _ in range(20):
            resp = _post(self.CTX, {"session_token": TOKEN, "phone": P_DUTCHIE}, WEBSITE)
            self.assertEqual((resp.status_code, resp.json()["first_name"]), (200, "Jordan"))


@override_settings(CACHES=_LOCMEM)
class MergeTests(TestCase):
    def setUp(self):
        self.primary = CustomerProfile.objects.create(
            phone=P_DUTCHIE, name="Jordan Smith", dutchie_ids=["77"], total_orders=5,
            purchase_history=[{"product_id": "p1", "sku": "s1", "times_bought": 5}])
        self.shell = CustomerProfile.objects.create(phone=P_SHELL, source="voice", name="Jo")
        ScanCustomer.objects.create(phone="5095550102", dutchie_acct_id=77)

    def test_a_shell_on_another_number_folds_into_the_dutchie_row_on_a_shared_account_id(self):
        sess = ChatSession.objects.create(session_token="vc-9", customer=self.shell, phone=P_SHELL)
        SuggestedProduct.objects.create(session=sess, customer=self.shell, location_slug="yakima", sku="s2")
        self.assertEqual(identity.merge_duplicates(), {"merged": 1, "ambiguous": 0, "skipped": 0})
        self.shell.refresh_from_db()
        self.assertEqual(self.shell.merged_into, self.primary)
        sess.refresh_from_db()
        self.assertEqual(sess.customer, self.primary)
        self.assertEqual(SuggestedProduct.objects.get().customer, self.primary)
        self.assertEqual(identity.profile_for_phone(P_SHELL), self.primary)  # either number, one person
        self.assertEqual(identity.profile_for_phone(P_DUTCHIE), self.primary)
        self.assertEqual(identity.merge_duplicates()["merged"], 0)  # idempotent

    def test_the_dutchie_name_wins_but_a_missing_one_is_filled_from_the_shell(self):
        self.primary.name = ""
        self.primary.save()
        identity.merge_duplicates()
        self.primary.refresh_from_db()
        self.assertEqual(self.primary.name, "Jo")

    def test_a_staff_claimed_order_is_also_evidence(self):
        ScanCustomer.objects.all().delete()
        PhoneCartDraft.objects.create(location_slug="yakima", contact_phone="509-555-0102", dutchie_acct_id="77")
        self.assertEqual(identity.merge_duplicates()["merged"], 1)

    def test_no_shared_account_id_means_no_merge_even_with_the_same_name(self):
        ScanCustomer.objects.all().delete()
        self.shell.name = "Jordan Smith"
        self.shell.save()
        self.assertEqual(identity.merge_duplicates()["merged"], 0)

    def test_it_fails_closed_on_two_dutchie_owners_or_a_shell_with_history(self):
        CustomerProfile.objects.create(phone="+15095550103", dutchie_ids=["77"], total_orders=1)
        self.assertEqual(identity.merge_duplicates(), {"merged": 0, "ambiguous": 1, "skipped": 0})
        self.shell.refresh_from_db()
        self.assertIsNone(self.shell.merged_into)

    def test_a_row_with_purchases_of_its_own_is_never_folded(self):
        self.shell.purchase_history = [{"sku": "z", "times_bought": 1}]
        self.shell.total_orders = 1
        self.shell.save()
        self.assertEqual(identity.merge_duplicates(), {"merged": 0, "ambiguous": 0, "skipped": 1})

    @patch("budtender.tasks.classify_products", lambda *a, **k: None)
    def test_dutchie_later_reporting_the_shells_number_lands_on_the_merged_row(self):
        identity.merge_duplicates()
        t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
        tx = {"customerId": "77", "transactionDate": t0.isoformat(),
              "items": [{"productId": "P9", "quantity": 1, "unitPrice": 10.0}]}
        cust = [{"customerId": "77", "cellPhone": "509-555-0102", "firstName": "Jordan", "lastName": "Smith"}]
        with patch("budtender.dutchie.get_customers", lambda slug: cust), \
             patch("budtender.dutchie.get_transactions_detailed", lambda *a, **k: [tx]):
            tasks.sync_transactions("yakima")
        self.assertEqual(CustomerProfile.objects.count(), 2)  # no third row, no resurrected duplicate
        self.primary.refresh_from_db()
        self.assertIn("P9", {h["product_id"] for h in self.primary.purchase_history})

    @patch("budtender.tasks.classify_products", lambda *a, **k: None)
    def test_sync_records_the_dutchie_ids_the_merge_depends_on(self):
        CustomerProfile.objects.all().delete()
        t0 = datetime(2026, 1, 5, tzinfo=timezone.utc)
        tx = {"customerId": "77", "transactionDate": t0.isoformat(),
              "items": [{"productId": "P1", "quantity": 1, "unitPrice": 10.0}]}
        cust = [{"customerId": "77", "cellPhone": "509-555-0101"}]
        with patch("budtender.dutchie.get_customers", lambda slug: cust), \
             patch("budtender.dutchie.get_transactions_detailed", lambda *a, **k: [tx]):
            tasks.sync_transactions("yakima")
            tasks.sync_transactions("yakima")  # a re-run changes nothing
        row = CustomerProfile.objects.get()
        self.assertEqual((row.source, row.dutchie_ids), ("dutchie", ["77"]))

    def test_the_weekly_task_is_scheduled_and_runs_the_merge(self):
        from core.celery import app

        entry = app.conf.beat_schedule["merge-duplicate-profiles-weekly"]
        self.assertEqual((entry["task"], entry["schedule"]), ("budtender.tasks.merge_duplicate_profiles", 604800.0))
        self.assertEqual(tasks.merge_duplicate_profiles()["merged"], 1)
