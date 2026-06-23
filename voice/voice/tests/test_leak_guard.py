"""tests/test_leak_guard.py — THE mandatory P1 gate (ADR-008; 11-P1 B2; 21-SPEC H1).

No ``"cost"`` / ``"margin"`` / ``"velocity"`` / ``"bucket"`` substring may appear in ANY tool
response the voice repo hands back — ``suggest_products`` / ``check_inventory`` / ``pair_upsell`` —
even when the (recorded) budtender response DELIBERATELY carries server-only fields. Two walls:
budtender never serializes them (its allowlist) AND the voice repo's ``_speakable_pick`` re-
allowlists + the central ``dispatch`` scrub. This proves the agent is STRUCTURALLY incapable of
speaking cost/margin. Offline, budtender stubbed.
"""

from __future__ import annotations

import json

import pytest

from voice import budtender_client, guardrails
from voice.tools import dispatch, suggest

FORBIDDEN = ("cost", "margin", "velocity", "bucket", "price_z", "margin_pct")

# A budtender row that (illegally) leaked server-only fields — proves the voice wall strips them.
_LEAKY_ROW = {
    "rank": 1,
    "sku": "PP-BBOG-35",
    "name": "Blueberry OG 3.5g",
    "brand": "Phat Panda",
    "strain": "Blueberry OG",
    "price": 38.0,
    "thc_percent": 27.3,
    "why_this": "indica for sleep",
    # server-only fields that must NEVER reach the agent:
    "cost": 18.0,
    "margin": 20.0,
    "margin_pct": 0.52,
    "velocity": 3.4,
    "bucket": "profit",
    "price_z": 0.7,
}


class FakeBudtender:
    def __init__(self, results=None, pairing=None, check=None):
        self.results = results or []
        self.pairing = pairing
        self.check = check

    def search(self, slots, **kw):
        return {"results": self.results}

    def pair_for_sku(self, store, anchor_sku, **kw):
        return self.pairing or {"pairing": None, "strength": 0.0}

    def check_sku(self, store, sku, **kw):
        return self.check or {"in_stock": False}


@pytest.fixture
def leaky_bt(monkeypatch):
    fb = FakeBudtender(
        results=[_LEAKY_ROW],
        pairing={
            "pairing": dict(_LEAKY_ROW, sku="WYLD", name="Gummies", price=12.0),
            "reason_text": "easy add-on",
            "strength": 0.7,
        },
        check={
            "in_stock": True,
            "price_otd": 41.0,
            "stock_on_hand": 9,
            "name": "X",
            "cost": 18.0,
            "margin": 20.0,
        },
    )
    monkeypatch.setattr(budtender_client, "budtender", lambda: fb)
    monkeypatch.setattr(suggest, "budtender", lambda: fb)
    return fb


def _assert_no_forbidden(obj):
    blob = json.dumps(obj).lower()
    for token in FORBIDDEN:
        assert token not in blob, f"forbidden token {token!r} leaked: {obj}"


# ── handler-level (the _speakable_pick wall) ────────────────────────────────────
def test_suggest_handler_strips_server_fields(leaky_bt):
    out = suggest.handle_suggest_products(
        {"store": "yakima", "category": "flower"}, {"call_id": "", "store": "yakima"}
    )
    _assert_no_forbidden(out)
    guardrails.assert_no_leak(out)


def test_check_handler_strips_server_fields(leaky_bt):
    out = suggest.handle_check_inventory({"store": "yakima", "sku": "X"}, {"store": "yakima"})
    _assert_no_forbidden(out)


def test_pair_handler_strips_server_fields(leaky_bt):
    out = suggest.handle_pair_upsell({"store": "yakima", "anchor_sku": "A"}, {"store": "yakima"})
    _assert_no_forbidden(out)


# ── dispatch-level (the central scrub wall, the structural guarantee) ───────────
def test_dispatch_suggest_is_leak_free(leaky_bt):
    out = dispatch(
        "suggest_products",
        {"store": "yakima", "category": "flower"},
        {"call_id": "", "store": "yakima"},
    )
    _assert_no_forbidden(out)
    guardrails.assert_no_leak(out)


def test_dispatch_check_is_leak_free(leaky_bt):
    out = dispatch("check_inventory", {"store": "yakima", "sku": "X"}, {"store": "yakima"})
    _assert_no_forbidden(out)


def test_dispatch_pair_is_leak_free(leaky_bt):
    out = dispatch("pair_upsell", {"store": "yakima", "anchor_sku": "A"}, {"store": "yakima"})
    _assert_no_forbidden(out)
