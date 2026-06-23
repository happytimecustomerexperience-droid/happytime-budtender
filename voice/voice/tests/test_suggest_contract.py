"""tests/test_suggest_contract.py — the tool→budtender round-trip through the webhook (11-P1 §7.2).

Drives a real ``tool-calls`` Vapi message through the HMAC-verified webhook with budtender stubbed
by a recorded response. Asserts: ≤3 in-stock picks each with a non-empty ``why_this`` + ``price_otd``
(B1); the anonymous path sends NO ``phone`` and the known path sends one (B3 — the voice repo only
controls send/omit; budtender owns re-ranking); HMAC fail-closed re-asserted on the tool path (H1).
Offline, no live keys.
"""

from __future__ import annotations

import json

import pytest

from voice import budtender_client, signing
from voice.tools import suggest

WEBHOOK_URL = "/api/voice/vapi"
SECRET = "test-webhook-secret-0123456789"

_RECORDED_RESULTS = [
    {
        "rank": 1,
        "sku": "PP1",
        "name": "Blueberry OG 3.5g",
        "brand": "Phat Panda",
        "strain": "Blueberry OG",
        "price": 38.0,
        "price_was": 43.0,
        "thc_percent": 27.3,
        "dominant_terpene": "Limonene",
        "stock_on_hand": 14,
        "dutchie_link": "/x",
        "image_url": "https://x",
        "why_this": "Indica-dominant — folks grab it for sleep",
    },
    {
        "rank": 2,
        "sku": "GG2",
        "name": "GG4 3.5g",
        "brand": "Gold Leaf",
        "strain": "GG4",
        "price": 30.0,
        "price_was": None,
        "thc_percent": 24.1,
        "dominant_terpene": "Myrcene",
        "stock_on_hand": 8,
        "dutchie_link": "/y",
        "image_url": None,
        "why_this": "Heavy-hitting hybrid",
    },
]


@pytest.fixture(autouse=True)
def _webhook_secret(settings):
    settings.VAPI_WEBHOOK_SECRET = SECRET
    settings.VAPI_SIGNATURE_HEADER = "X-Vapi-Signature"
    settings.VAPI_SECRET_HEADER = "X-Vapi-Secret"
    settings.HHT_DEFAULT_STORE = "yakima"


class RecordingBudtender:
    """Records the search call (so we can assert phone presence) and replays the recorded set."""

    def __init__(self, profile_has_history=False):
        self.profile_has_history = profile_has_history
        self.search_calls: list[dict] = []

    def resume_by_phone(self, phone_e164, *, location=None, current_session_token=None):
        return {
            "session_token": "s-known" if self.profile_has_history else None,
            "profile_summary": {
                "has_history": self.profile_has_history,
                "top_categories": ["flower"],
                "price_tier": "mid",
            },
        }

    def search(
        self, slots, *, limit=3, phone=None, session_token=None, exclude_skus=None, location=None
    ):
        self.search_calls.append({"phone": phone, "limit": limit, "session_token": session_token})
        return {"results": _RECORDED_RESULTS[:limit]}

    def pair_for_sku(self, *a, **kw):
        return {"pairing": None, "strength": 0.0}


def _patch_bt(monkeypatch, bt):
    monkeypatch.setattr(budtender_client, "budtender", lambda: bt)
    monkeypatch.setattr(suggest, "budtender", lambda: bt)
    import voice.recognition as recog

    monkeypatch.setattr(recog, "budtender", lambda: bt, raising=False)


def _post_signed(client, payload):
    raw = json.dumps(payload).encode()
    sig = signing.compute_signature(raw, SECRET)
    return client.post(
        WEBHOOK_URL, data=raw, content_type="application/json", **{"HTTP_X_VAPI_SIGNATURE": sig}
    )


def _suggest_payload(call_id, number=None):
    call = {"id": call_id}
    if number:
        call["customer"] = {"number": number}
    return {
        "message": {
            "type": "tool-calls",
            "call": call,
            "toolCalls": [
                {
                    "id": "tc1",
                    "function": {
                        "name": "suggest_products",
                        "arguments": {
                            "store": "yakima",
                            "category": "flower",
                            "effect_desired": "relaxed",
                            "price_max": 40,
                        },
                    },
                }
            ],
        }
    }


# ── B1. ≤3 in-stock picks, non-empty why_this, price_otd present ────────────────
@pytest.mark.django_db
def test_suggest_contract_returns_speakable_picks(client, monkeypatch):
    _patch_bt(monkeypatch, RecordingBudtender(profile_has_history=False))
    resp = _post_signed(client, _suggest_payload("c_anon"))
    assert resp.status_code == 200
    result = resp.json()["results"][0]["result"]
    picks = result["picks"]
    assert 1 <= len(picks) <= 3
    for p in picks:
        assert p["why_this"]  # non-empty spoken reason
        assert p["price_otd"] > 0  # OTD present
        assert "cost" not in json.dumps(p) and "margin" not in json.dumps(p)
    assert result["spoken_summary"]


# ── B3. anonymous → NO phone ; known → phone sent ───────────────────────────────
@pytest.mark.django_db
def test_anonymous_path_omits_phone(client, monkeypatch):
    bt = RecordingBudtender(profile_has_history=False)
    _patch_bt(monkeypatch, bt)
    # an UNRECOGNIZED number → resume_by_phone returns has_history:false → no phone on search
    _post_signed(client, _suggest_payload("c_anon2", number="+15095550000"))
    assert bt.search_calls[0]["phone"] is None  # W_ANON (margin-first)


@pytest.mark.django_db
def test_known_path_sends_phone(client, monkeypatch):
    bt = RecordingBudtender(profile_has_history=True)
    _patch_bt(monkeypatch, bt)
    _post_signed(client, _suggest_payload("c_known", number="+15095551234"))
    assert bt.search_calls[0]["phone"] == "+15095551234"  # W_KNOWN (taste-first)


@pytest.mark.django_db
def test_blocked_number_is_anonymous(client, monkeypatch):
    bt = RecordingBudtender(profile_has_history=True)  # would HIT if a number were present
    _patch_bt(monkeypatch, bt)
    _post_signed(client, _suggest_payload("c_blocked"))  # no customer.number
    assert bt.search_calls[0]["phone"] is None


# ── H1. HMAC fail-closed re-asserted on the tool path ───────────────────────────
@pytest.mark.django_db
def test_tool_path_rejects_bad_signature(client, monkeypatch):
    _patch_bt(monkeypatch, RecordingBudtender())
    raw = json.dumps(_suggest_payload("c_bad")).encode()
    resp = client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SIGNATURE": "deadbeef" * 8},
    )
    assert resp.status_code == 401  # rejected before the handler runs


@pytest.mark.django_db
def test_tool_path_rejects_missing_signature(client, monkeypatch):
    _patch_bt(monkeypatch, RecordingBudtender())
    raw = json.dumps(_suggest_payload("c_nosig")).encode()
    resp = client.post(WEBHOOK_URL, data=raw, content_type="application/json")
    assert resp.status_code == 401
