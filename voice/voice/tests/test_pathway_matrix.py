"""Pathway matrix: one owner-facing table proving EVERY conversational pathway the voice
agent can take is exercised before a live test, plus a drift guard so a newly-registered
tool can never ship silently uncovered.

This file does NOT re-test individual tool behavior (see test_suggest_tools.py,
test_vendor_callback_tool.py, test_n8n_tool.py, etc. for that — 363 tests already do it).
It tests ROUTING: does `voice.chat.answer_text_chat` (the shared brain both Vapi and
website chat call through) send each kind of customer turn down the right path, and does
every tool in `TOOL_REGISTRY` have at least one exercised scenario anywhere in this file.

Offline/key-free throughout (conftest.py conventions): Gemini/Vapi/budtender/SMTP are all
mocked or never touched.
"""

from __future__ import annotations

import json
import re

import pytest

from voice import chat
from voice.tools import TOOL_REGISTRY, dispatch

# ── registry-coverage guard ──────────────────────────────────────────────────────────
# Every tool name below has an exercised scenario in this file (either through the shared
# brain in PATHWAY_CASES, or as a direct-dispatch scenario further down for tools the
# website-chat brain never reaches on its own, e.g. vendor/escalation/n8n/phone-cart).
# When a 9th tool is registered, this set must grow — see test_registry_covers_every_tool.
COVERED_TOOLS = {
    "faq_lookup",
    "suggest_products",
    "check_inventory",
    "pair_upsell",
    "stage_phone_cart",
    "notify_vendor_callback",
    "notify_staff_issue",
    "notify_n8n",
}


def test_registry_covers_every_tool():
    missing = set(TOOL_REGISTRY) - COVERED_TOOLS
    assert not missing, (
        f"tool(s) {sorted(missing)} are registered in TOOL_REGISTRY but have no scenario in "
        "voice/tests/test_pathway_matrix.py. Add a case to PATHWAY_CASES (if reachable through "
        "voice.chat.answer_text_chat) or a direct-dispatch scenario near the bottom of this "
        "file, then add the tool name to COVERED_TOOLS."
    )


# ── a recording fake dispatch standing in for the two tools the chat brain calls ────────
# Mirrors test_all_in_one_conversation.py's style: fake dispatch, real chat.answer_text_chat.
class _RecordingDispatch:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, tool, args, ctx):
        self.calls.append((tool, dict(args)))
        if tool == "faq_lookup":
            return self._faq(str(args.get("query") or "").lower())
        if tool == "suggest_products":
            return self._suggest(args)
        return {}

    @staticmethod
    def _faq(q: str) -> dict:
        if "return" in q or "refund" in q:
            return {
                "grounded": True,
                "answer": "Unopened items may be returned within 24 hours; defective items are "
                "reviewed by staff under WAC 314-55-079.",
                "sources": [{"kind": "policy", "title": "Return policy",
                             "source_url": "https://happytimeweed.com/faq"}],
            }
        if "special" in q or "deal" in q:
            return {
                "grounded": True,
                "answer": "This week: 15% off all edibles storewide.",
                "sources": [{"kind": "faq", "title": "Specials",
                             "source_url": "https://happytimeweed.com/specials"}],
            }
        if "hour" in q or "open" in q or "location" in q:
            return {
                "grounded": True,
                "answer": "We're open 9am to 9pm daily, seven days a week.",
                "sources": [{"kind": "store_fact", "title": "Store hours",
                             "source_url": "https://happytimeweed.com/hours"}],
            }
        return {"grounded": False, "fallback": "I can't confirm that from the current Happy Time knowledge base."}

    @staticmethod
    def _suggest(args: dict) -> dict:
        category = str(args.get("category") or "product")
        return {
            "picks": [{"name": f"{category.title()} Sample Pick", "why_this": f"matches your {category} request"}],
            "spoken_summary": f"Here are a couple of {category} options in stock.",
        }


@pytest.fixture
def recording(monkeypatch):
    fake = _RecordingDispatch()
    monkeypatch.setattr(chat, "dispatch", fake)
    return fake


def _has_digit(text: str) -> bool:
    return bool(re.search(r"\d", text or ""))


# ── the pathway table ────────────────────────────────────────────────────────────────
# Each case: (id, message, extra kwargs to answer_text_chat, expected `out` subset, optional
# extra assertion callback receiving (out, recording)).
PATHWAY_CASES = [
    pytest.param(
        "grounded_faq_hours",
        "what are your hours today",
        {},
        {"grounded": True, "intent": "hours_location"},
        None,
        id="1-grounded-faq-hours",
    ),
    pytest.param(
        "return_policy_faq",
        "what is your return policy",
        {},
        {"grounded": True, "intent": "return_policy"},
        None,
        id="2-return-policy-faq",
    ),
    pytest.param(
        "specials_faq",
        "any specials or deals this week",
        {},
        {"grounded": True, "intent": "specials"},
        None,
        id="3-specials-faq",
    ),
    pytest.param(
        "product_flower",
        "I'm looking for an indica flower",
        {},
        {"intent": "product_suggestion"},
        None,
        id="4a-product-flower",
    ),
    pytest.param(
        "product_edible",
        "do you have any gummy edibles",
        {},
        {"intent": "product_suggestion"},
        None,
        id="4b-product-edible",
    ),
    pytest.param(
        "product_cartridge",
        "show me some vape cartridges",
        {},
        {"intent": "product_suggestion"},
        None,
        id="4c-product-cartridge",
    ),
    pytest.param(
        "product_concentrate",
        "looking for some concentrate wax",
        {},
        {"intent": "product_suggestion"},
        None,
        id="4d-product-concentrate",
    ),
    pytest.param(
        "product_preroll",
        "I'd like a pre-roll please",
        {},
        {"intent": "product_suggestion"},
        None,
        id="4e-product-pre-roll",
    ),
    pytest.param(
        "price_ceiling_free_text",
        "I need some flower under $40",
        {},
        {"intent": "product_suggestion"},
        lambda out, rec: _assert_price_max_reached(rec, 40.0),
        id="5-price-ceiling-parsed-into-suggest-args",
    ),
    pytest.param(
        "escalation_broken_cart",
        "this cart is broken, I want a human",
        {},
        {
            "escalation_required": True,
            "intent": "conflict_resolution",
            "safe_next_action": "escalate",
        },
        None,
        id="6-escalation-angry-customer",
    ),
    pytest.param(
        "numbers_guard_ungrounded_policy",
        "what is your compliance policy on ID checks",
        {},
        {"grounded": False},
        lambda out, rec: _assert_no_invented_number(out),
        id="7-numbers-guard-no-grounded-hit",
    ),
    pytest.param(
        # FIXED: an empty/whitespace-only message used to short-circuit with a bare
        # {"ok": False, "error": ...} envelope — no answer, no intent label. It now degrades the
        # same safe way as any other unrecognized input (an honest ungrounded fallback).
        "empty_message",
        "",
        {},
        {"ok": True, "grounded": False},
        None,
        id="9-empty-message",
    ),
]


def _assert_price_max_reached(recording: _RecordingDispatch, expected: float) -> None:
    suggest_calls = [args for tool, args in recording.calls if tool == "suggest_products"]
    assert suggest_calls, "suggest_products was never dispatched"
    assert suggest_calls[0].get("price_max") == expected


def _assert_no_invented_number(out: dict) -> None:
    # Numbers-Guard: an ungrounded answer must never contain a digit (price/stock/hour/etc.).
    assert not _has_digit(out["answer"]), f"ungrounded answer invented a number: {out['answer']!r}"


@pytest.mark.parametrize("_name,message,kwargs,expected,extra", PATHWAY_CASES)
def test_pathway(recording, _name, message, kwargs, expected, extra):
    payload = {"message": message, **kwargs}
    out = chat.answer_text_chat(payload)
    for key, value in expected.items():
        assert out.get(key) == value, f"{_name}: expected {key}={value!r}, got {out.get(key)!r} (out={out!r})"
    if extra is not None:
        extra(out, recording)


# ── pathway 8: leak-guard — no tool result ever carries cost/margin ─────────────────
def test_leak_guard_scrubs_cost_and_margin_through_the_real_dispatch(monkeypatch):
    """Patches the REGISTRY (not chat.dispatch) so this exercises the real
    voice.tools.dispatch -> guardrails.scrub_leak wall the shared brain relies on, with a
    tool handler that deliberately "leaks" the way a regression would."""

    def _leaky_faq(args, ctx):
        return {"answer": "leaked", "grounded": True, "sources": [], "cost": 12.5, "margin": "38% margin"}

    def _leaky_suggest(args, ctx):
        return {
            "picks": [{"name": "X", "cost": 5, "why_this": "y"}],
            "spoken_summary": "ok",
            "margin_pct": 0.4,
        }

    monkeypatch.setitem(TOOL_REGISTRY, "faq_lookup", _leaky_faq)
    monkeypatch.setitem(TOOL_REGISTRY, "suggest_products", _leaky_suggest)

    out = chat.answer_text_chat({"message": "I'm looking for some flower", "store": "yakima"})

    assert out["tool_results"], "no tool results captured"
    for entry in out["tool_results"]:
        blob = json.dumps(entry["result"]).lower()
        assert "cost" not in blob
        assert "margin" not in blob


# ── direct-dispatch scenarios for tools not covered by PATHWAY_CASES above ──────────
# FIXED 2026-08-10: `answer_text_chat` now also reaches `stage_phone_cart` and
# `notify_vendor_callback` itself (see voice/chat.py's vendor/staging gates + the thread 06/07/18
# conversation tests) — a website chat visitor CAN trigger a vendor callback or a cart staging.
# `notify_staff_issue`/`notify_n8n` remain Vapi-only handoffs the text brain never calls. Every
# tool in TOOL_REGISTRY still gets at least one offline smoke scenario here — proving it's wired,
# returns a spoken envelope, and never leaks cost/margin — without duplicating the deep per-tool
# suites that already cover them.


class _FakeBudtender:
    """Minimal offline stand-in for voice.budtender_client.BudtenderClient."""

    def __init__(self):
        self.check_result = {"in_stock": True, "price_otd": 24.5, "stock_on_hand": 9, "name": "Blue Dream 1g"}
        self.pair_result = {"pairing": None, "strength": 0.0}
        self.phone_cart_result = {"ok": True, "draft": {"draft_token": "tok123", "quote": {"total": 24.5, "discounts": 0}}}

    def check_sku(self, store, sku, *, category=None):
        return self.check_result

    def pair_for_sku(self, store, anchor_sku, *, phone=None, session_token=None):
        return self.pair_result

    def phone_cart_upsert(self, payload):
        return self.phone_cart_result

    def phone_cart_release(self, payload):
        return self.phone_cart_result


@pytest.fixture
def fake_budtender(monkeypatch):
    from voice import budtender_client
    from voice.tools import phone_cart, suggest

    fb = _FakeBudtender()
    monkeypatch.setattr(budtender_client, "budtender", lambda: fb)
    monkeypatch.setattr(suggest, "budtender", lambda: fb)
    monkeypatch.setattr(phone_cart, "budtender", lambda: fb)
    return fb


def test_check_inventory_pathway(fake_budtender):
    out = dispatch("check_inventory", {"store": "yakima", "sku": "SKU1"}, {"store": "yakima"})
    assert out["in_stock"] is True
    assert "cost" not in out and "margin" not in out


def test_pair_upsell_pathway_silent_below_gate(fake_budtender):
    # strength 0.0 < PAIR_STRENGTH_GATE → the agent stays silent (a correct, not-a-bug outcome).
    out = dispatch("pair_upsell", {"store": "yakima", "anchor_sku": "SKU1"}, {"store": "yakima"})
    assert out == {"offer": False}


def test_stage_phone_cart_pathway(fake_budtender):
    out = dispatch(
        "stage_phone_cart",
        {"action": "quote", "store": "yakima"},
        {"store": "yakima", "call_id": "call-1", "_caller_phone": "+15095551234"},
    )
    assert out["ok"] is True
    assert "24.50" in out["spoken_summary"] or "24.5" in out["spoken_summary"]
    blob = json.dumps(out).lower()
    assert "cost" not in blob and "margin" not in blob


@pytest.fixture
def _email_cfg(settings):
    settings.STAFF_ALERT_EMAIL = "staff@happytimeweed.com"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.HHT_DEFAULT_STORE = "yakima"
    settings.HHT_VENDOR_CALLBACK_WINDOW = "one business day"
    settings.PHONE_HASH_PEPPER = "test-pepper-pathway-matrix"


@pytest.mark.django_db
def test_notify_vendor_callback_pathway(_email_cfg):
    out = dispatch(
        "notify_vendor_callback",
        {"store": "yakima", "reason": "delivery", "summary": "no one answered the warm transfer"},
        {"call_id": "pm-vendor-1", "store": "yakima", "caller_number": "+15095551212"},
    )
    assert out["logged"] is True
    assert out["callback_window"] == "one business day"
    blob = json.dumps(out).lower()
    assert "cost" not in blob and "margin" not in blob


@pytest.mark.django_db
def test_notify_staff_issue_pathway(_email_cfg):
    out = dispatch(
        "notify_staff_issue",
        {"store": "yakima", "issue_type": "defective_return", "summary": "cart won't fire, wants a refund"},
        {"call_id": "pm-escalation-1", "store": "yakima"},
    )
    assert out["logged"] is True
    assert out["alerted"] is True
    blob = json.dumps(out).lower()
    assert "cost" not in blob and "margin" not in blob


def test_notify_n8n_pathway_degrades_offline(settings):
    # No N8N_WEBHOOK_URL configured in the test env → degrade-safe, no network attempted.
    settings.N8N_WEBHOOK_URL = ""
    out = dispatch("notify_n8n", {"event_type": "send_menu_link"}, {"store": "yakima"})
    assert out["ok"] is False
    assert out["reason"] == "n8n not configured"
