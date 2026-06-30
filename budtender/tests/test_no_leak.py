"""
Regression test: no cost/margin EVER appears in a client-facing response.
This is the hard guarantee behind the proxy + allowlist serializer design.
"""
import json

from django.test import Client, TestCase, override_settings

from budtender.models import CustomerProfile, Product
from budtender.serializers import (PUBLIC_PRODUCT_FIELDS, customer_detail,
                                   public_product)

TOKEN = "test-token"
FORBIDDEN = ("margin", "cost")


def _make_product(**kw):
    defaults = dict(
        sku="SKU1", location_slug="yakima", slug="blue-dream", name="Blue Dream",
        brand="Acme", category="flower", strain="Blue Dream", strain_type="hybrid",
        price=30, cost=12, margin=18, quantity_on_hand=5, availability=True,
    )
    defaults.update(kw)
    return Product.objects.create(**defaults)


class PublicSerializerTests(TestCase):
    def test_public_product_has_only_allowlisted_fields(self):
        p = _make_product()
        out = public_product(p, rank=1)
        self.assertEqual(set(out.keys()), set(PUBLIC_PRODUCT_FIELDS))

    def test_public_product_never_contains_cost_or_margin(self):
        p = _make_product()
        blob = json.dumps(public_product(p)).lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, blob, f"'{word}' leaked into public product")


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class SearchEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        _make_product(sku="A", price=25, cost=10, margin=15)
        _make_product(sku="B", slug="og-kush", name="OG Kush", price=35, cost=12, margin=23)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

    def test_requires_token(self):
        r = self.client.post("/api/v1/products/search/", data={}, content_type="application/json")
        self.assertEqual(r.status_code, 403)

    def test_search_response_excludes_cost_margin(self):
        r = self.client.post(
            "/api/v1/products/search/",
            data=json.dumps({"slots": {"store": "yakima", "category": "flower"}, "limit": 5}),
            content_type="application/json",
            **self._auth(),
        )
        self.assertEqual(r.status_code, 200)
        body = r.content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body, f"'{word}' leaked into /products/search response")

    def test_search_bad_limit_falls_back_without_leak(self):
        r = self.client.post(
            "/api/v1/products/search/",
            data=json.dumps({"slots": {"store": "yakima", "category": "flower"}, "limit": "bad"}),
            content_type="application/json",
            **self._auth(),
        )

        self.assertEqual(r.status_code, 200)
        body = r.content.decode().lower()
        for word in FORBIDDEN:
            self.assertNotIn(word, body)

    def test_search_honors_sanitized_request_ranking_weights(self):
        _make_product(
            sku="ANCHOR", location_slug="pullman", slug="anchor", name="House Hybrid",
            price=30, cost=1, margin=29,
        )
        _make_product(
            sku="EFFECT", location_slug="pullman", slug="effect", name="Limonene Haze",
            strain="Haze", strain_type="sativa", price=30, cost=25, margin=5,
        )
        _make_product(
            sku="PROFIT", location_slug="pullman", slug="profit", name="Plain Flower",
            price=30, cost=12, margin=18,
        )
        slots = {"store": "pullman", "category": "flower", "effect_desired": "uplifted"}

        effect_heavy = self.client.post(
            "/api/v1/products/search/",
            data=json.dumps({
                "slots": slots,
                "ranking_weights": {"w_anon": {"margin": 0, "effect": 1}, "margin_emphasis": 1},
            }),
            content_type="application/json",
            **self._auth(),
        ).json()["results"]
        margin_heavy = self.client.post(
            "/api/v1/products/search/",
            data=json.dumps({
                "slots": slots,
                "ranking_weights": {"w_anon": {"margin": 1, "effect": 0}, "margin_emphasis": 1},
            }),
            content_type="application/json",
            **self._auth(),
        ).json()["results"]

        self.assertEqual(effect_heavy[1]["sku"], "EFFECT")
        self.assertEqual(margin_heavy[1]["sku"], "PROFIT")

    def test_health_is_public(self):
        r = self.client.get("/api/v1/health/")
        self.assertEqual(r.status_code, 200)

    def test_customer_list_and_detail_no_leak(self):
        CustomerProfile.objects.create(
            phone="+15095551212", name="Jane Doe", total_orders=5, price_tier="top",
            category_affinity={"flower": 0.7, "edible": 0.3}, brand_affinity={"Acme": 1.0},
            bucket_mix={"core": 0.6, "profit": 0.4},
            purchase_history=[{"sku": "A", "brand": "Acme", "category": "flower",
                               "times_bought": 4, "last_price": 30, "price_z": 0.5}],
        )
        # serializer is leak-safe
        p = CustomerProfile.objects.get(phone="+15095551212")
        self.assertNotIn("margin", json.dumps(customer_detail(p)).lower())
        self.assertNotIn("cost", json.dumps(customer_detail(p)).lower())

        # list requires the token, searches by name, no leak
        self.assertEqual(self.client.post("/api/v1/customer/list", data="{}",
                         content_type="application/json").status_code, 403)
        lst = self.client.post("/api/v1/customer/list", data=json.dumps({"q": "jane"}),
                               content_type="application/json", **self._auth())
        self.assertEqual(lst.status_code, 200)
        self.assertEqual(lst.json()["total"], 1)
        self.assertEqual(lst.json()["customers"][0]["name"], "Jane Doe")
        for word in FORBIDDEN:
            self.assertNotIn(word, lst.content.decode().lower())

        # detail by phone returns favorites, no leak; missing → 404
        det = self.client.post("/api/v1/customer/detail", data=json.dumps({"phone": "+15095551212"}),
                               content_type="application/json", **self._auth())
        self.assertEqual(det.status_code, 200)
        self.assertEqual(det.json()["customer"]["name"], "Jane Doe")
        self.assertTrue(det.json()["customer"]["favorites"])
        for word in FORBIDDEN:
            self.assertNotIn(word, det.content.decode().lower())
        miss = self.client.post("/api/v1/customer/detail", data=json.dumps({"phone": "+15550009999"}),
                                content_type="application/json", **self._auth())
        self.assertEqual(miss.status_code, 404)

    def test_by_sku_returns_one_in_stock_product_no_leak(self):
        r = self.client.get(
            "/api/v1/products/by-sku/", {"store": "yakima", "sku": "A"}, **self._auth()
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["product"]["sku"], "A")
        for word in FORBIDDEN:
            self.assertNotIn(word, json.dumps(body).lower())

    def test_by_sku_missing_returns_empty(self):
        r = self.client.get(
            "/api/v1/products/by-sku/", {"store": "yakima", "sku": "NOPE"}, **self._auth()
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {})

    def test_by_sku_requires_token(self):
        r = self.client.get("/api/v1/products/by-sku/", {"store": "yakima", "sku": "A"})
        self.assertEqual(r.status_code, 403)

    def test_admin_ranking_weights_requires_token_and_normalizes(self):
        unauth = self.client.post(
            "/api/v1/admin/ranking-weights",
            data=json.dumps({"w_anon": {"margin": 2}, "margin_emphasis": "bad"}),
            content_type="application/json",
        )
        self.assertEqual(unauth.status_code, 403)

        r = self.client.post(
            "/api/v1/admin/ranking-weights",
            data=json.dumps({
                "w_anon": {"margin": 2, "effect": 1, "bad": 999},
                "w_known": {"affinity": 3, "margin": 1},
                "margin_emphasis": "bad",
            }),
            content_type="application/json",
            **self._auth(),
        )

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(set(body["applied"]), {"w_anon", "w_known", "margin_emphasis"})
        self.assertAlmostEqual(sum(body["applied"]["w_anon"].values()), 1.0)
        self.assertAlmostEqual(sum(body["applied"]["w_known"].values()), 1.0)
        self.assertNotIn("bad", body["applied"]["w_anon"])
        self.assertEqual(body["applied"]["margin_emphasis"], 1.0)
