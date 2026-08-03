"""Wiring an online order to a Dutchie customer.

`cart_submit` refuses to run without an AcctId, so an order that reaches the
register with nobody attached is a dead end — the budtender has to re-find the
customer by hand with the shopper standing there. These tests pin the resolution.
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from budtender.models import PhoneCartDraft
from bundles import customers

CACHES_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _draft(**kw):
    d = dict(location_slug="yakima", source=PhoneCartDraft.Source.ONLINE,
             status=PhoneCartDraft.Status.RELEASED, pickup_name="Sam Reyes",
             contact_phone="5095551212", contact_email="sam@example.com")
    d.update(kw)
    return PhoneCartDraft.objects.create(**d)


def _search(*rows):
    return {"Data": list(rows)}


@override_settings(CACHES=CACHES_LOCMEM)
class SplitNameTests(TestCase):
    def test_splits_first_and_last(self):
        self.assertEqual(customers.split_name("Sam Reyes"), ("Sam", "Reyes"))

    def test_single_token_becomes_the_first_name(self):
        self.assertEqual(customers.split_name("Cher"), ("Cher", ""))

    def test_multi_word_surname_stays_together(self):
        self.assertEqual(customers.split_name("Ana Maria de la Cruz"),
                         ("Ana", "Maria de la Cruz"))

    def test_blank(self):
        self.assertEqual(customers.split_name("   "), ("", ""))


@override_settings(CACHES=CACHES_LOCMEM)
class LookupTests(TestCase):
    def _patch(self, client):
        return patch("bundles.customers._client", return_value=client)

    def test_matches_on_digits_not_string_form(self):
        # Dutchie stores whatever shape the guest was created with; a string
        # compare would miss the same person.
        client = MagicMock()
        client.guest_search.return_value = _search(
            {"Guest_id": 4242, "Name": "Sam Reyes", "PhoneNo": "(509) 555-1212"})
        with self._patch(client):
            acct, name, status = customers.lookup_by_phone("yakima", "+1 509 555 1212")
        self.assertEqual(acct, "4242")
        self.assertEqual(name, "Sam Reyes")
        self.assertEqual(status, PhoneCartDraft.Customer.MATCHED)

    def test_a_near_miss_phone_is_not_a_match(self):
        client = MagicMock()
        client.guest_search.return_value = _search(
            {"Guest_id": 1, "Name": "Someone Else", "PhoneNo": "5095559999"})
        with self._patch(client):
            acct, _, status = customers.lookup_by_phone("yakima", "5095551212")
        self.assertEqual(acct, "")
        self.assertEqual(status, PhoneCartDraft.Customer.NEW)

    def test_empty_result_means_new_customer(self):
        client = MagicMock()
        client.guest_search.return_value = _search()
        with self._patch(client):
            _, _, status = customers.lookup_by_phone("yakima", "5095551212")
        self.assertEqual(status, PhoneCartDraft.Customer.NEW)

    def test_dutchie_outage_is_unresolved_not_new(self):
        # Critical distinction: "we couldn't check" must never be mistaken for
        # "no account", or we'd create a duplicate guest for an existing customer.
        client = MagicMock()
        client.guest_search.side_effect = RuntimeError("dutchie down")
        with self._patch(client):
            _, _, status = customers.lookup_by_phone("yakima", "5095551212")
        self.assertEqual(status, PhoneCartDraft.Customer.UNRESOLVED)

    def test_blank_phone_is_unresolved(self):
        _, _, status = customers.lookup_by_phone("yakima", "")
        self.assertEqual(status, PhoneCartDraft.Customer.UNRESOLVED)


@override_settings(CACHES=CACHES_LOCMEM)
class AttachTests(TestCase):
    def test_stamps_the_match_onto_the_draft(self):
        draft = _draft()
        client = MagicMock()
        client.guest_search.return_value = _search(
            {"Guest_id": 77, "Name": "Sam Reyes", "PhoneNo": "5095551212"})
        with patch("bundles.customers._client", return_value=client):
            customers.attach(draft)
        self.assertEqual(draft.dutchie_acct_id, "77")
        self.assertEqual(draft.customer_status, PhoneCartDraft.Customer.MATCHED)


@override_settings(CACHES=CACHES_LOCMEM)
class EnsureCustomerTests(TestCase):
    def test_an_existing_match_is_reused_without_another_call(self):
        draft = _draft(dutchie_acct_id="99", customer_name="Sam Reyes",
                       customer_status=PhoneCartDraft.Customer.MATCHED)
        client = MagicMock()
        with patch("bundles.customers._client", return_value=client):
            acct, name, how = customers.ensure_customer(draft)
        self.assertEqual((acct, how), ("99", "matched"))
        client.guest_search.assert_not_called()
        client.create_guest.assert_not_called()

    def test_rechecks_before_creating_so_a_walk_in_is_not_duplicated(self):
        # The shopper may have been created at the door between ordering and the
        # budtender claiming. A duplicate guest is worse than a wasted lookup.
        draft = _draft(customer_status=PhoneCartDraft.Customer.NEW)
        client = MagicMock()
        client.guest_search.return_value = _search(
            {"Guest_id": 555, "Name": "Sam Reyes", "PhoneNo": "5095551212"})
        with patch("bundles.customers._client", return_value=client):
            acct, _, how = customers.ensure_customer(draft)
        self.assertEqual((acct, how), ("555", "matched"))
        client.create_guest.assert_not_called()

    def test_creates_when_there_is_genuinely_no_account(self):
        draft = _draft(customer_status=PhoneCartDraft.Customer.NEW)
        client = MagicMock()
        client.guest_search.return_value = _search()
        client.create_guest.return_value = 8080
        with patch("bundles.customers._client", return_value=client):
            acct, _, how = customers.ensure_customer(draft)
        self.assertEqual((acct, how), ("8080", "created"))
        kwargs = client.create_guest.call_args.kwargs
        self.assertEqual(kwargs["first_name"], "Sam")
        self.assertEqual(kwargs["last_name"], "Reyes")
        self.assertEqual(kwargs["phone"], "5095551212")
        self.assertEqual(kwargs["dob"], "")   # never collected online

    def test_never_creates_when_the_lookup_failed(self):
        draft = _draft()
        client = MagicMock()
        client.guest_search.side_effect = RuntimeError("down")
        with patch("bundles.customers._client", return_value=client):
            acct, _, how = customers.ensure_customer(draft)
        self.assertEqual((acct, how), ("", "failed"))
        client.create_guest.assert_not_called()

    def test_create_failure_degrades_instead_of_raising(self):
        draft = _draft()
        client = MagicMock()
        client.guest_search.return_value = _search()
        client.create_guest.side_effect = RuntimeError("boom")
        with patch("bundles.customers._client", return_value=client):
            self.assertEqual(customers.ensure_customer(draft)[2], "failed")

    def test_no_name_means_no_create(self):
        draft = _draft(pickup_name="")
        client = MagicMock()
        client.guest_search.return_value = _search()
        with patch("bundles.customers._client", return_value=client):
            self.assertEqual(customers.ensure_customer(draft)[2], "failed")
        client.create_guest.assert_not_called()
