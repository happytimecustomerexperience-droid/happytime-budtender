"""The public site's own token opens only the views it calls (budtender/auth.py).

A leak of the website's environment must not read the customer roster, chat transcripts, or a
profile by phone — those need the voice service's HHT_BACKEND_TOKEN.
"""
import json

from django.test import Client, TestCase, override_settings

from budtender import views

BACKEND = "backend-token"
WEBSITE = "website-token"
SITE_PATHS = ("/api/v1/products/price-bands", "/api/v1/products/sizes")
STAFF_ONLY = (
    "/api/v1/customer/list",
    "/api/v1/customer/detail",
    "/api/v1/chat/history",
    "/api/v1/chat/resume-by-phone",
    "/api/v1/phone-cart/claim",
    "/api/v1/customer/profile-upsert",
    "/api/v1/customer/caller-context",
    "/api/v1/customer/memory/learn",
    "/api/v1/customer/memory/clear",
    "/api/v1/admin/ranking-weights",
    "/api/v1/analytics/funnel",
    "/api/v1/analytics/session",
    "/api/v1/analytics/suggestions",
    "/api/v1/analytics/suggestions/list",
    "/api/v1/customer/suggestions",
)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE)
class WebsiteTokenTests(TestCase):
    def _call(self, path, token):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
        if path in SITE_PATHS:
            return Client().get(path + "?location=yakima", **auth)
        return Client().post(path, data=json.dumps({}), content_type="application/json", **auth)

    def test_the_website_token_opens_the_sites_own_views(self):
        for path in SITE_PATHS:
            self.assertNotIn(self._call(path, WEBSITE).status_code, (401, 403), path)

    def test_the_website_token_is_refused_everywhere_else(self):
        for path in STAFF_ONLY:
            self.assertIn(self._call(path, WEBSITE).status_code, (401, 403), path)

    def test_the_backend_token_still_opens_everything(self):
        for path in STAFF_ONLY:
            self.assertNotIn(self._call(path, BACKEND).status_code, (401, 403), path)

    def test_a_wrong_token_opens_nothing(self):
        for path in SITE_PATHS + STAFF_ONLY:
            self.assertIn(self._call(path, "nope").status_code, (401, 403), path)

    def test_no_customer_or_transcript_view_is_marked_for_the_website(self):
        for view in (views.CustomerListView, views.CustomerDetailView, views.ChatHistoryView,
                     views.ResumeByPhoneView, views.PhoneCartClaimView, views.ProfileUpsertView,
                     views.CallerContextView, views.MemoryLearnView, views.MemoryClearView,
                     views.AnalyticsSuggestionsView, views.AnalyticsSuggestionsListView,
                     views.CustomerSuggestionsView):
            self.assertFalse(getattr(view, "website_ok", False), view.__name__)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN="")
class NoWebsiteTokenTests(TestCase):
    def test_an_unset_website_token_matches_nothing(self):
        resp = Client().get(SITE_PATHS[0] + "?location=yakima", HTTP_AUTHORIZATION="Bearer ")
        self.assertIn(resp.status_code, (401, 403))


VICTIM = "+15095551234"


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE)
class TypedPhoneIsNotIdentityTests(TestCase):
    """A website visitor TYPED the phone, so it must never resolve a customer: budtender
    personalises from a profile ("your go-to {brand}"), which would let anyone read a stranger's
    favourites by typing their number. Only the backend token (the voice service's carrier
    caller-ID) may."""

    def setUp(self):
        from budtender.models import CustomerProfile

        self.victim = CustomerProfile.objects.create(phone=VICTIM)

    def _post(self, path, payload, token):
        return Client().post(path, data=json.dumps(payload), content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {token}")

    def _search_profile(self, token):
        from unittest.mock import patch

        seen = []
        # No inventory in the test DB reads as stale, and the async refresh then waits on a broker.
        with patch("budtender.views.inventory_is_stale", return_value=False), \
             patch("budtender.views.rank_products", side_effect=lambda loc, slots, profile, **kw: seen.append(profile) or []):
            resp = self._post("/api/v1/products/search/", {"slots": {"store": "yakima"}, "phone": VICTIM}, token)
        self.assertEqual(resp.status_code, 200, resp.content[:200])
        return seen[0]

    def test_search_with_the_website_token_is_anonymous(self):
        self.assertIsNone(self._search_profile(WEBSITE))
        self.assertEqual(self._search_profile(BACKEND), self.victim)  # control: the voice service

    def test_pairing_with_the_website_token_is_anonymous(self):
        from unittest.mock import patch

        seen = []

        def spy(location, anchor, profile):
            seen.append(profile)
            return None, "none", "", 0.0

        with patch("budtender.views.pair_for", side_effect=spy):
            for token in (WEBSITE, BACKEND):
                self._post("/api/v1/pairing/for-sku", {"location": "yakima", "sku": "x", "phone": VICTIM}, token)
        self.assertEqual(seen, [None, self.victim])

    def test_chat_and_persist_never_link_a_session_to_a_typed_phone(self):
        from unittest.mock import patch

        from budtender.models import ChatSession

        with patch("budtender.views.generate_chat_reply_with_source", return_value=("hi", "brain", "")):
            self._post("/api/v1/chat/message", {"message": "hi", "phone": VICTIM, "location": "yakima"}, WEBSITE)
        self._post("/api/v1/chat/persist/", {"session_token": "s-" + "a" * 32, "phone": VICTIM,
                                              "messages": []}, WEBSITE)
        self.assertFalse(ChatSession.objects.filter(customer=self.victim).exists())
        self.assertFalse(ChatSession.objects.filter(phone=VICTIM).exists())
