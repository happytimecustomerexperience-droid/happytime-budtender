"""Personalized-feed robustness: the cached taste profile + live in-session taste blend.

The promise: EVERY customer (new / guest / DB-down) gets a feed that personalizes the
moment they view or add anything — without a happytime profile and without a per-render
DB hit.
"""

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import RequestFactory
from django.urls import reverse

from pos import ranking
from pos import views as V
from customers import intelligence as I
from customers import tracking
from dutchie.session import Store

pytestmark = pytest.mark.django_db

STORE = Store(name="yakima", base_url="https://bo", pos_base_url="https://pos",
              org_id=700002, lsp_id=700045, loc_id=700498, register_id=700318,
              username="u", password="p", api_key="k")


@pytest.fixture
def auth(client, db):
    client.force_login(User.objects.create_user("bud", password="pw12345!"))
    return client


def _use_store(monkeypatch):
    monkeypatch.setattr(V, "load_stores", lambda: {STORE.name: STORE})


def _req():
    r = RequestFactory().get("/")
    r.session = {}
    return r


def _item(cat, cat_key, brand, st, margin, price):
    return {
        "product_id": f"{brand}-{cat}", "name": f"{brand} {cat}", "brand": brand,
        "category": cat, "raw_category": cat, "cat_key": cat_key, "cat_label": cat.title(),
        "strain": "", "strain_type": st, "terpene": "", "effects": [], "flavors": [],
        "thc": 22, "cbd": 0, "total_terpenes": 0, "price": price, "price_was": 0,
        "qty": 8, "image": "", "img": None, "img_static": True, "received_date": "2026-06-20",
        "vendor": "", "unit_grams": 1, "bucket": "core", "velocity": 1.0,
        "margin_pct": margin, "price_z": 0.0, "subcategory": "",
        "ProductId": 1, "BatchId": 2, "SerialNo": "S", "UnitPrice": price,
        "RecUnitPrice": price, "ProductDesc": f"{brand} {cat}", "CannbisProduct": "Yes",
    }


# ── profile cache (speed + resilience) ────────────────────────────────────────
def test_profile_cache_serves_second_call(monkeypatch):
    cache.clear()
    calls = []
    monkeypatch.setattr(I, "load_profile_full", lambda phone: calls.append(phone) or {"orders": 1})
    a = I.load_profile_full_cached("5095550100")
    b = I.load_profile_full_cached("5095550100")
    assert a == b == {"orders": 1} and len(calls) == 1   # 2nd served from cache


def test_profile_cache_negative_caches_none(monkeypatch):
    cache.clear()
    calls = []
    monkeypatch.setattr(I, "load_profile_full", lambda phone: calls.append(phone) and None)
    assert I.load_profile_full_cached("5090001111") is None
    assert I.load_profile_full_cached("5090001111") is None
    assert len(calls) == 1   # a down/empty DB isn't hammered per render


def test_profile_cache_empty_phone():
    assert I.load_profile_full_cached("") is None


# ── live in-session taste blend ───────────────────────────────────────────────
def test_blend_builds_profile_from_taste_alone():
    eff = ranking.blend_session_taste(None, {"category": {"Flower": 3}, "brand": {"PP": 1}})
    assert eff and eff["category_affinity"]["Flower"] > 0 and eff["brand_affinity"]["PP"] > 0


def test_blend_no_taste_returns_profile_unchanged():
    prof = {"category_affinity": {"Vaporizer": 0.5}}
    assert ranking.blend_session_taste(prof, None) is prof
    assert ranking.blend_session_taste(prof, {}) is prof
    assert ranking.blend_session_taste(None, {}) is None


def test_blend_boosts_existing_capped():
    eff = ranking.blend_session_taste({"category_affinity": {"Flower": 0.2}},
                                      {"category": {"Flower": 9}})
    assert 0.2 < eff["category_affinity"]["Flower"] <= 1.0


def test_session_only_personalizes_ranking():
    """A new customer (no profile) who's been eyeing Flower-A gets it first even though the
    vape has higher margin."""
    inv = [_item("Flower", "flower", "A", "hybrid", 0.50, 40),
           _item("Vaporizer", "vapes", "B", "indica", 0.55, 25)]
    taste = {"category": {"Flower": 5}, "brand": {"A": 5}, "strain_type": {"hybrid": 5}}
    eff = ranking.blend_session_taste(None, taste)
    ranked = ranking.rank(inv, eff)
    assert ranked[0]["category"] == "Flower"


# ── taste accrual ─────────────────────────────────────────────────────────────
def test_known_customer_taste_beats_unbought_high_margin_phat_panda():
    inv = [_item("Flower", "flower", "Loved Brand", "hybrid", 0.30, 40),
           _item("Flower", "flower", "Phat Panda", "indica", 0.90, 40)]
    profile = {
        "brand_affinity": {"Loved Brand": 1.0},
        "strain_type_affinity": {"hybrid": 1.0},
        "purchase_history": [{"brand": "Loved Brand", "category": "Flower"}],
    }

    ranked = ranking.rank(inv, profile)

    assert ranked[0]["brand"] == "Loved Brand"


def test_accrue_taste_accumulates_weighted():
    r = _req()
    p = {"category": "Flower", "brand": "Phat Panda", "strain_type": "hybrid"}
    tracking.accrue_taste(r, p, 1)
    tracking.accrue_taste(r, p, 3)
    t = r.session["taste"]
    assert t["category"]["Flower"] == 4 and t["brand"]["Phat Panda"] == 4


def test_accrue_taste_skips_blanks():
    r = _req()
    tracking.accrue_taste(r, None, 1)
    tracking.accrue_taste(r, {"category": "", "brand": None}, 1)
    assert r.session["taste"] == {}


# ── integration: the menu personalizes from session taste alone (no phone/profile) ──
def test_menu_personalizes_from_session_taste(auth, monkeypatch):
    cache.clear()
    _use_store(monkeypatch)
    inv = [_item("Flower", "flower", "A", "hybrid", 0.50, 40),
           _item("Vaporizer", "vapes", "B", "indica", 0.55, 25)]
    monkeypatch.setattr(V.catalog, "get_inventory", lambda store: inv)
    s = auth.session
    s["acct_id"] = 1
    s["acct_name"] = "Jane"            # NO acct_phone -> no happytime profile at all
    s["taste"] = {"category": {"Flower": 5}, "brand": {"A": 5}, "strain_type": {"hybrid": 5}}
    s.save()
    r = auth.get(reverse("menu") + "?sort=foryou", SERVER_NAME="localhost")
    assert r.status_code == 200 and b"sorted for Jane" in r.content   # personalized w/o profile


# ── hidden-data signals: flavor + THC band ────────────────────────────────────
def test_thc_band_fit_in_out_unknown():
    prof = {"thc_min": 25, "thc_max": 30}
    assert ranking.thc_band_fit({"thc": 27}, prof) == 1.0
    assert 0 < ranking.thc_band_fit({"thc": 20}, prof) < 1.0
    assert ranking.thc_band_fit({"thc": 27}, {}) == 0.0          # no band -> no signal
    assert ranking.thc_band_fit({"thc": None}, prof) == 0.0


def test_flavor_affinity_lifts_score():
    a = _item("Flower", "flower", "A", "hybrid", 0.5, 40)
    b = _item("Flower", "flower", "B", "hybrid", 0.5, 40)
    a["flavors"], b["flavors"] = ["berry"], ["pine"]
    ranked = ranking.rank([a, b], {"flavor_affinity": {"berry": 0.9}})
    assert ranked[0]["brand"] == "A"                            # flavor match wins at equal margin


def test_thc_band_lifts_score():
    a = _item("Flower", "flower", "A", "hybrid", 0.5, 40)
    b = _item("Flower", "flower", "B", "hybrid", 0.5, 40)
    a["thc"], b["thc"] = 27, 15
    ranked = ranking.rank([a, b], {"thc_min": 25, "thc_max": 30})
    assert ranked[0]["brand"] == "A"                            # in-band wins at equal margin


def test_menu_renders_per_category_carousels(auth, monkeypatch):
    cache.clear()
    _use_store(monkeypatch)
    inv = [_item("Flower", "flower", "A", "hybrid", 0.5, 40),
           _item("Vaporizer", "vapes", "B", "indica", 0.5, 25)]
    monkeypatch.setattr(V.catalog, "get_inventory", lambda store: inv)
    s = auth.session
    s["acct_id"] = 1
    s["acct_name"] = "Jane"
    s["taste"] = {"category": {"flower": 5}}
    s.save()
    r = auth.get(reverse("menu"), SERVER_NAME="localhost")
    assert r.status_code == 200 and b"for Jane" in r.content and b"Flower" in r.content
