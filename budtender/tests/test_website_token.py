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
    "/api/v1/admin/ranking-weights",
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
                     views.ResumeByPhoneView, views.PhoneCartClaimView, views.ProfileUpsertView):
            self.assertFalse(getattr(view, "website_ok", False), view.__name__)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN="")
class NoWebsiteTokenTests(TestCase):
    def test_an_unset_website_token_matches_nothing(self):
        resp = Client().get(SITE_PATHS[0] + "?location=yakima", HTTP_AUTHORIZATION="Bearer ")
        self.assertIn(resp.status_code, (401, 403))
