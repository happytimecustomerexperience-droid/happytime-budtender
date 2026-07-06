"""No cost/margin/velocity/price_z EVER reaches the staff-facing POS HTML.

Mirrors the API no-leak guard (budtender/tests/test_no_leak.py) for the in-store
screens. The enrichment the POS joins in (margin_pct, velocity, price_z, bucket)
is scoring-only and must never render. HTML legitimately contains the CSS word
"margin", so we plant unmistakable SENTINEL values in the enrichment and assert
those exact sentinels — and the sensitive field-name keys — never appear.
"""
import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from dutchie.session import Store
from pos import views as V

pytestmark = pytest.mark.django_db

# Distinctive so they can't collide with real prices/THC/CSS in the page.
MARGIN_SENTINEL = "0.7777"
VELOCITY_SENTINEL = "88.88"
PRICE_Z_SENTINEL = "13.37"
BUCKET_SENTINEL = "secretbucket"
FORBIDDEN = ("margin_pct", "price_z", "velocity",
             MARGIN_SENTINEL, VELOCITY_SENTINEL, PRICE_Z_SENTINEL, BUCKET_SENTINEL)

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=1, lsp_id=1, loc_id=1, register_id=1, username="u", password="p", api_key="k")


def _enriched_item():
    return {
        "product_id": "1", "ProductId": 1, "name": "Blue Dream", "brand": "Acme",
        "category": "Flower", "raw_category": "Flower", "cat_key": "flower", "cat_label": "Flower",
        "strain": "Blue Dream", "strain_type": "hybrid", "terpene": "limonene", "thc": 27, "cbd": 0,
        "total_terpenes": 0, "price": 40, "price_was": 45, "qty": 8, "image": "", "img": None,
        "img_static": True, "received_date": "2026-06-20", "vendor": "", "unit_grams": 3.5,
        "effects": [], "flavors": [], "potency_mg": None, "subcategory": "3.5g",
        # sensitive enrichment (scoring-only) — must NEVER render:
        "bucket": BUCKET_SENTINEL, "velocity": float(VELOCITY_SENTINEL),
        "margin_pct": float(MARGIN_SENTINEL), "price_z": float(PRICE_Z_SENTINEL),
        "UnitPrice": 40, "RecUnitPrice": 40, "ProductDesc": "Blue Dream", "CannbisProduct": "Yes",
    }


@pytest.fixture
def auth(client, db, monkeypatch):
    monkeypatch.setattr(V, "load_stores", lambda: {STORE.name: STORE})
    monkeypatch.setattr(V.catalog, "get_inventory", lambda store: [_enriched_item()])
    client.force_login(User.objects.create_user("bud", password="pw12345!", is_staff=True))
    return client


def _assert_clean(resp):
    body = resp.content.decode().lower()
    for w in FORBIDDEN:
        assert w.lower() not in body, f"'{w}' leaked into POS HTML"


def _shopping(client):
    s = client.session
    s["acct_id"] = 1
    s["acct_name"] = "Jane"
    s.save()


def test_menu_no_leak(auth):
    _shopping(auth)
    r = auth.get(reverse("menu"), SERVER_NAME="localhost")
    assert r.status_code == 200
    _assert_clean(r)


def test_product_detail_no_leak(auth):
    _shopping(auth)
    r = auth.get(reverse("product", args=["1"]), SERVER_NAME="localhost")
    assert r.status_code == 200
    _assert_clean(r)
