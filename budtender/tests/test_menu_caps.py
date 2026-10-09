"""Per-shopper-IP caps on the menu reads (search, facets family, similar).

The website's server proxies every shopper, so the cap keys on the IP it vouches for in
``X-HHT-Client-IP``. The voice service (backend token) has no client IP and is never capped.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import Client, TestCase, override_settings

from budtender import views

BACKEND, WEBSITE = "backend-token", "website-token"
_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
BODY = {"store": "yakima", "category": "flower", "facet": "thc", "slots": {"store": "yakima"}, "sku": "nope"}
PATHS = [
    "/api/v1/products/search/",
    "/api/v1/products/facets",
    "/api/v1/products/categories",
    "/api/v1/products/specify-more",
    "/api/v1/products/similar",
]


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    monkeypatch.setattr("budtender.views.fire", lambda *a, **k: False)
    monkeypatch.setattr("budtender.views.inventory_is_stale", lambda *a, **k: False)
    monkeypatch.setattr("budtender.views.rank_products", lambda *a, **k: [])


def _post(path, token=WEBSITE, **headers):
    return Client().post(path, data=json.dumps(BODY), content_type="application/json",
                         HTTP_AUTHORIZATION=f"Bearer {token}", **headers)


@override_settings(HHT_BACKEND_TOKEN=BACKEND, HHT_WEBSITE_TOKEN=WEBSITE, CACHES=_LOCMEM)
class MenuCapTests(TestCase):
    def test_every_menu_read_is_capped_per_vouched_visitor_ip(self):
        for path in PATHS:
            with self.subTest(path=path), patch("budtender.views.MENU_REQUESTS_PER_IP", 2):
                ip = {"HTTP_X_HHT_CLIENT_IP": f"203.0.113.{PATHS.index(path) + 10}"}
                codes = [_post(path, **ip).status_code for _ in range(3)]
                self.assertEqual(codes[:2], [200, 200])
                self.assertEqual(codes[2], 429)

    def test_the_refusal_carries_retry_after_and_another_visitor_is_unaffected(self):
        with patch("budtender.views.MENU_REQUESTS_PER_IP", 1):
            _post(PATHS[0], HTTP_X_HHT_CLIENT_IP="203.0.113.7")
            refused = _post(PATHS[0], HTTP_X_HHT_CLIENT_IP="203.0.113.7")
            other = _post(PATHS[0], HTTP_X_HHT_CLIENT_IP="198.51.100.9")
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(refused["Retry-After"], str(views.MENU_WINDOW))
        self.assertEqual(other.status_code, 200)

    def test_the_budget_is_shared_across_the_menu_endpoints(self):
        ip = {"HTTP_X_HHT_CLIENT_IP": "203.0.113.20"}
        with patch("budtender.views.MENU_REQUESTS_PER_IP", 2):
            codes = [_post(PATHS[0], **ip).status_code, _post(PATHS[1], **ip).status_code,
                     _post(PATHS[4], **ip).status_code]
        self.assertEqual(codes, [200, 200, 429])

    def test_the_backend_token_is_never_capped(self):
        with patch("budtender.views.MENU_REQUESTS_PER_IP", 1):
            codes = [_post(p, BACKEND, HTTP_X_HHT_CLIENT_IP="203.0.113.30").status_code
                     for p in PATHS * 2]
        self.assertNotIn(429, codes)

    def test_no_or_garbage_ip_header_is_not_capped(self):
        with patch("budtender.views.MENU_REQUESTS_PER_IP", 1):
            codes = [_post(PATHS[0]).status_code for _ in range(3)]
            codes += [_post(PATHS[0], HTTP_X_HHT_CLIENT_IP="not-an-ip").status_code for _ in range(3)]
        self.assertNotIn(429, codes)

    def test_the_limit_is_generous_for_a_real_shopper(self):
        # 3 requests a second sustained for a minute: paging + facet chips never gets near this.
        self.assertGreaterEqual(views.MENU_REQUESTS_PER_IP, 120)
