"""voice/tools/suggest.py — the three tool handlers (11-P1 §3.2; AC B/C/D).

budtender is MOCKED (a fake client returns recorded leak-safe responses). Offline, no network, no
key. Asserts arg validation, the leak-safe → speakable mapper (allowlist + OTD relabel), honest-
empty, the pairing strength gate at the threshold, and the margin-vs-taste request switch.
"""

from __future__ import annotations

import pytest

from voice import budtender_client
from voice.tools import suggest


# ── a fake budtender client recording what the handlers send ────────────────────
class FakeBudtender:
    def __init__(self, search_results=None, pairing=None, check=None):
        self.search_results = search_results if search_results is not None else []
        self.pairing = pairing
        self.check = check
        self.search_calls: list[dict] = []
        self.pair_calls: list[dict] = []

    def search(
        self, slots, *, limit=3, phone=None, session_token=None, exclude_skus=None, location=None
    ):
        self.search_calls.append(
            {"slots": slots, "limit": limit, "phone": phone, "session_token": session_token}
        )
        return {"results": self.search_results[:limit]}

    def pair_for_sku(self, store, anchor_sku, *, phone=None, session_token=None):
        self.pair_calls.append({"store": store, "anchor": anchor_sku, "phone": phone})
        return self.pairing or {"pairing": None, "strength": 0.0}

    def check_sku(self, store, sku, *, category=None):
        return self.check or {"in_stock": False}


@pytest.fixture
def fake_bt(monkeypatch):
    fb = FakeBudtender()
    monkeypatch.setattr(budtender_client, "budtender", lambda: fb)
    # The handlers import budtender into their own namespace; patch there too.
    monkeypatch.setattr(suggest, "budtender", lambda: fb)
    return fb


# A leak-safe budtender result row (the public_product shape — no cost/margin).
_ROW = {
    "rank": 1,
    "sku": "PP-BBOG-35",
    "name": "Blueberry OG 3.5g",
    "brand": "Phat Panda",
    "strain": "Blueberry OG",
    "price": 38.0,
    "price_was": 43.0,
    "thc_percent": 27.3,
    "dominant_terpene": "Limonene",
    "stock_on_hand": 14,
    "dutchie_link": "/catalog/product/x",
    "image_url": "https://x/y.jpg",
    "why_this": "Indica-dominant — folks grab it for sleep",
}


# ── B1. valid call → ≤3 picks, in-stock, why_this, price_otd ────────────────────
def test_suggest_returns_speakable_picks(fake_bt):
    fake_bt.search_results = [_ROW]
    out = suggest.handle_suggest_products(
        {"store": "yakima", "category": "flower", "effect_desired": "relaxed", "price_max": 40},
        {"call_id": "", "store": "yakima"},
    )
    assert len(out["picks"]) == 1
    pick = out["picks"][0]
    assert pick["name"] == "Blueberry OG 3.5g"
    assert pick["why_this"]  # non-empty spoken reason
    assert pick["price_otd"] == 56.43  # 38 * Yakima OTD; relabeled from price
    assert out["spoken_summary"]  # a real spoken lead-in


def test_speakable_pick_drops_non_allowlist_fields():
    pick = suggest._speakable_pick(_ROW, "yakima")
    assert set(pick) == {
        "rank",
        "name",
        "brand",
        "strain",
        "thc_percent",
        "why_this",
        "sku",
        "price_otd",
    }
    # the raw pre-tax price + image/dutchie_link/stock/price_was are all dropped
    assert "price" not in pick
    assert "image_url" not in pick and "dutchie_link" not in pick
    assert "stock_on_hand" not in pick and "price_was" not in pick


def test_suggest_caps_at_three(fake_bt):
    fake_bt.search_results = [dict(_ROW, sku=f"S{i}") for i in range(6)]
    out = suggest.handle_suggest_products(
        {"store": "yakima", "category": "flower"}, {"call_id": "", "store": "yakima"}
    )
    assert len(out["picks"]) == 3


# ── B1. required-field validation (clear tool error, not a 500) ─────────────────
def test_suggest_missing_category_is_tool_error(fake_bt):
    out = suggest.handle_suggest_products({"store": "yakima"}, {"call_id": "", "store": "yakima"})
    assert out["error"] == "missing_category"
    assert out["picks"] == []


def test_suggest_defaults_store_to_yakima(fake_bt):
    fake_bt.search_results = [_ROW]
    suggest.handle_suggest_products({"category": "flower"}, {"call_id": ""})
    assert fake_bt.search_calls[0]["slots"]["store"] == "yakima"


# ── B4. honest-empty ────────────────────────────────────────────────────────────
def test_suggest_honest_empty_when_no_results(fake_bt):
    fake_bt.search_results = []
    out = suggest.handle_suggest_products(
        {"store": "yakima", "category": "flower"}, {"call_id": "", "store": "yakima"}
    )
    assert out["picks"] == []
    assert "not finding" in out["spoken_summary"].lower()


# ── B3. margin-vs-taste switch (anonymous omits phone; known sends it) ──────────
def test_anonymous_caller_omits_phone(fake_bt):
    fake_bt.search_results = [_ROW]
    # ctx already resolved as anonymous (no _caller_phone)
    ctx = {"call_id": "", "store": "yakima", "recognition_resolved": True}
    suggest.handle_suggest_products({"store": "yakima", "category": "flower"}, ctx)
    assert fake_bt.search_calls[0]["phone"] is None  # → W_ANON (margin-first)


def test_known_caller_sends_phone(fake_bt):
    fake_bt.search_results = [_ROW]
    ctx = {
        "call_id": "",
        "store": "yakima",
        "recognition_resolved": True,
        "_caller_phone": "+15095551234",
        "session_token": "s-known",
    }
    suggest.handle_suggest_products({"store": "yakima", "category": "flower"}, ctx)
    assert fake_bt.search_calls[0]["phone"] == "+15095551234"  # → W_KNOWN (taste-first)
    assert fake_bt.search_calls[0]["session_token"] == "s-known"


# ── C1. check_inventory ─────────────────────────────────────────────────────────
def test_check_inventory_in_stock(fake_bt):
    fake_bt.check = {"in_stock": True, "price_otd": 41.2, "stock_on_hand": 14, "name": "X"}
    out = suggest.handle_check_inventory({"store": "yakima", "sku": "SKU1"}, {"store": "yakima"})
    assert out["in_stock"] is True
    assert out["price_otd"] == 41.2
    assert out["qty_band"] == "in stock"
    assert "cost" not in out and "margin" not in out


def test_check_inventory_out_of_stock(fake_bt):
    fake_bt.check = {"in_stock": False}
    out = suggest.handle_check_inventory({"store": "yakima", "sku": "NOPE"}, {"store": "yakima"})
    assert out == {"in_stock": False}


def test_check_inventory_missing_sku(fake_bt):
    out = suggest.handle_check_inventory({"store": "yakima"}, {"store": "yakima"})
    assert out["error"] == "missing_sku"
    assert out["in_stock"] is False


# ── D1/D2. pairing strength gate at the threshold ───────────────────────────────
def _pairing(strength):
    return {
        "pairing": {
            "sku": "WYLD-10",
            "name": "Sleep gummies",
            "brand": "Wyld",
            "price": 12.0,
            "thc_percent": None,
            "strain": None,
            "rank": 1,
            "why_this": "easy add-on",
        },
        "reason_code": "popular_pair",
        "reason_text": "Folks grab a low-dose gummy — an easy add-on.",
        "strength": strength,
    }


def test_pair_offers_when_strength_clears_gate(fake_bt):
    fake_bt.pairing = _pairing(0.62)
    out = suggest.handle_pair_upsell({"store": "yakima", "anchor_sku": "A"}, {"store": "yakima"})
    assert out["offer"] is True
    assert out["pair"]["name"] == "Sleep gummies"
    assert out["pair"]["price_otd"] > 0
    assert out["strength"] == 0.62


def test_pair_silent_below_gate(fake_bt):
    fake_bt.pairing = _pairing(0.39)  # just below 0.40
    out = suggest.handle_pair_upsell({"store": "yakima", "anchor_sku": "A"}, {"store": "yakima"})
    assert out == {"offer": False}


def test_pair_offers_exactly_at_gate(fake_bt):
    fake_bt.pairing = _pairing(0.40)
    out = suggest.handle_pair_upsell({"store": "yakima", "anchor_sku": "A"}, {"store": "yakima"})
    assert out["offer"] is True


def test_pair_silent_when_null(fake_bt):
    fake_bt.pairing = {"pairing": None, "strength": 0.9}
    out = suggest.handle_pair_upsell({"store": "yakima", "anchor_sku": "A"}, {"store": "yakima"})
    assert out == {"offer": False}


def test_pair_missing_anchor(fake_bt):
    out = suggest.handle_pair_upsell({"store": "yakima"}, {"store": "yakima"})
    assert out["error"] == "missing_anchor_sku"
    assert out["offer"] is False


def test_pair_gate_constant_is_040():
    assert suggest.PAIR_STRENGTH_GATE == 0.40


# ── suggested-SKU stamping onto the durable VoiceCall (D4) ──────────────────────
@pytest.mark.django_db
def test_suggest_stamps_skus_on_voicecall(fake_bt):
    from voice.models import Outcome, VoiceCall

    fake_bt.search_results = [dict(_ROW, sku="S1"), dict(_ROW, sku="S2")]
    ctx = {
        "call_id": "call_stamp_1",
        "store": "yakima",
        "recognition_resolved": True,
        "caller_phone_hash": "h" * 64,
    }
    suggest.handle_suggest_products({"store": "yakima", "category": "flower"}, ctx)
    vc = VoiceCall.objects.get(call_id="call_stamp_1")
    assert vc.outcome == Outcome.SUGGESTED
    assert vc.suggested_skus == ["S1", "S2"]
    assert vc.caller_phone_hash == "h" * 64  # only the hash, never a raw number
