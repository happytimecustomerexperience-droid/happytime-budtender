"""
Regression test: no cost/margin EVER appears in a client-facing response.
This is the hard guarantee behind the proxy + allowlist serializer design.
"""
import json

from django.test import Client, TestCase, override_settings

from budtender.models import Product
from budtender.serializers import PUBLIC_PRODUCT_FIELDS, public_product

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

    def test_health_is_public(self):
        r = self.client.get("/api/v1/health/")
        self.assertEqual(r.status_code, 200)

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
