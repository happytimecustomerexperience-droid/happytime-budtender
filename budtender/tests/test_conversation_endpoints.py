"""Staff endpoints behind the voice dashboard's customer conversations panel (T4):
``customer/call-ids`` (a customer's Vapi call ids from their ``vc-`` sessions) and
``customer/name-match`` (how many customers carry exactly this name). Backend token only."""
import json

from django.test import Client, TestCase, override_settings

from budtender.models import ChatSession, CustomerProfile

BACKEND, WEBSITE = "backend-token", "website-token"


def _post(path, payload, token=BACKEND):
    return Client().post(path, data=json.dumps(payload), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}")


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE)
class CallIdsTests(TestCase):
    URL = "/api/v1/customer/call-ids"

    def setUp(self):
        self.alice = CustomerProfile.objects.create(phone="+15095550111", name="Alice A")
        self.bob = CustomerProfile.objects.create(phone="+15095550122", name="Bob B")
        ChatSession.objects.create(session_token="vc-call-a1", channel="voice", customer=self.alice)
        ChatSession.objects.create(session_token="vc-call-a2", channel="voice", customer=self.alice)
        ChatSession.objects.create(session_token="vc-call-b1", channel="voice", customer=self.bob)
        # Alice's website chats and a stray token are never call ids.
        ChatSession.objects.create(session_token="s-" + "Q1w2E3r4T5" * 3, customer=self.alice)
        ChatSession.objects.create(session_token="web-vc-looking", customer=self.alice)

    def test_returns_only_that_customers_vc_ids(self):
        body = _post(self.URL, {"customer_id": self.alice.pk}).json()
        self.assertTrue(body["ok"])
        self.assertEqual(sorted(body["call_ids"]), ["call-a1", "call-a2"])
        self.assertEqual(body["total"], 2)
        self.assertNotIn("call-b1", json.dumps(body))
        self.assertNotIn("s-Q1w2", json.dumps(body))

    def test_unknown_or_missing_customer_is_an_empty_list(self):
        self.assertEqual(_post(self.URL, {"customer_id": 999999}).json()["call_ids"], [])
        self.assertEqual(_post(self.URL, {}).json()["call_ids"], [])

    def test_limit_is_bounded(self):
        body = _post(self.URL, {"customer_id": self.alice.pk, "limit": 1}).json()
        self.assertEqual(len(body["call_ids"]), 1)
        self.assertEqual(body["total"], 2)

    def test_website_token_is_refused(self):
        self.assertEqual(_post(self.URL, {"customer_id": self.alice.pk}, WEBSITE).status_code, 403)
        self.assertEqual(_post(self.URL, {"customer_id": self.alice.pk}, "nope").status_code, 403)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE)
class NameMatchTests(TestCase):
    URL = "/api/v1/customer/name-match"

    def setUp(self):
        self.one = CustomerProfile.objects.create(phone="+15095550111", name="Alice  Archer")
        self.dup_a = CustomerProfile.objects.create(phone="+15095550122", name="Sam Smith")
        self.dup_b = CustomerProfile.objects.create(phone="+15095550133", name="sam   smith")
        self.longer = CustomerProfile.objects.create(phone="+15095550144", name="Alice Archer Jr")

    def test_exact_name_ignoring_case_and_whitespace(self):
        body = _post(self.URL, {"name": "ALICE archer"}).json()
        self.assertEqual((body["count"], body["id"]), (1, self.one.pk))

    def test_substring_and_first_name_never_match(self):
        for name in ("Alice", "Archer", "Alice Arch", "Alice Archer Jr Sr"):
            body = _post(self.URL, {"name": name}).json()
            self.assertEqual(body["count"], 0, name)
            self.assertIsNone(body["id"])

    def test_ambiguous_name_reports_the_count_and_no_id(self):
        body = _post(self.URL, {"name": "Sam Smith"}).json()
        self.assertEqual(body["count"], 2)
        self.assertIsNone(body["id"])

    def test_every_candidate_is_compared_however_many_contain_both_words(self):
        CustomerProfile.objects.bulk_create(
            CustomerProfile(phone=f"+1509555{i:04d}", name=f"Maria X{i} Garcia") for i in range(500))
        CustomerProfile.objects.create(phone="+15095559998", name="Maria Garcia")
        CustomerProfile.objects.create(phone="+15095559999", name="maria  garcia")
        body = _post(self.URL, {"name": "Maria Garcia"}).json()
        self.assertEqual((body["count"], body["id"]), (2, None))  # ambiguous, never "unique"

    def test_a_merged_away_row_does_not_count(self):
        self.dup_b.merged_into = self.dup_a
        self.dup_b.save()
        body = _post(self.URL, {"name": "Sam Smith"}).json()
        self.assertEqual((body["count"], body["id"]), (1, self.dup_a.pk))

    def test_blank_name_matches_nothing(self):
        self.assertEqual(_post(self.URL, {"name": "   "}).json()["count"], 0)

    def test_no_phone_in_the_response(self):
        self.assertNotIn("+1509", json.dumps(_post(self.URL, {"name": "Alice Archer"}).json()))

    def test_website_token_is_refused(self):
        self.assertEqual(_post(self.URL, {"name": "Alice Archer"}, WEBSITE).status_code, 403)
