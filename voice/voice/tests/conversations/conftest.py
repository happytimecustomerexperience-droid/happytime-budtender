"""Harness for the full-conversation threads — the real KB, a fake budtender, one driver.

Offline and key-free like the rest of the suite (03-CONVENTIONS.md §5): the KB is the REAL
production seed (``kb.seed.seed_all``) so grounded answers are the ones a caller actually hears,
and budtender is a fake with a small but realistic catalog that honours category/price filters —
so "show me a cartridge under $40" has to actually route those slots to get a hit.

Usage in a thread module::

    def test_thread(convo):
        c = convo(store="yakima")
        t = c.say("what are your hours")
        assert t.grounded and t.intent == "hours_location"
        t = c.say("got any relaxing indica flower")
        assert t.picks and t.args("suggest_products")["category"] == "flower"
"""

from __future__ import annotations

import pytest

from voice import budtender_client, recognition
from voice.tools import phone_cart, suggest

# ── a small catalog that behaves like real inventory ──────────────────────────
# Leak-safe ``public_product`` rows (no cost/margin — budtender never sends those).
CATALOG = [
    {"sku": "FL-BBOG-35", "name": "Blueberry OG 3.5g", "brand": "Phat Panda", "strain": "Blueberry OG",
     "category": "flower", "subcategory": "indica", "size": "3.5g", "price": 38.0, "thc_percent": 27.3,
     "dominant_terpene": "Myrcene", "stock_on_hand": 14, "why_this": "Indica-dominant — folks grab it for sleep"},
    {"sku": "FL-GG4-35", "name": "Gorilla Glue #4 3.5g", "brand": "Grow Op", "strain": "GG4",
     "category": "flower", "subcategory": "hybrid", "size": "3.5g", "price": 30.0, "thc_percent": 24.1,
     "dominant_terpene": "Caryophyllene", "stock_on_hand": 22, "why_this": "Balanced hybrid, easy all-rounder"},
    {"sku": "FL-SD-28", "name": "Sour Diesel 28g", "brand": "Legacy", "strain": "Sour Diesel",
     "category": "flower", "subcategory": "sativa", "size": "28g", "price": 99.0, "thc_percent": 22.0,
     "dominant_terpene": "Limonene", "stock_on_hand": 6, "why_this": "Daytime sativa ounce deal"},
    {"sku": "ED-WYLD-10", "name": "Wyld Raspberry Gummies 10mg", "brand": "Wyld", "strain": None,
     "category": "edible", "subcategory": "gummies", "size": "10mg", "price": 15.0, "thc_percent": None,
     "dominant_terpene": None, "stock_on_hand": 40, "why_this": "Low-dose, predictable — good starting point"},
    {"sku": "ED-CQ-5", "name": "Cannaquench Sparkling 5mg", "brand": "Cannaquench", "strain": None,
     "category": "edible", "subcategory": "beverage", "size": "5mg", "price": 8.0, "thc_percent": None,
     "dominant_terpene": None, "stock_on_hand": 33, "why_this": "Microdose drink, fast onset"},
    {"sku": "CT-JETTY-1G", "name": "Jetty Blue Dream 1g Cart", "brand": "Jetty", "strain": "Blue Dream",
     "category": "cartridge", "subcategory": "sativa", "size": "1g", "price": 35.0, "thc_percent": 84.0,
     "dominant_terpene": "Pinene", "stock_on_hand": 18, "why_this": "Uplifting daytime cart, clean hardware"},
    {"sku": "CT-AV-05", "name": "Avitas GSC 0.5g Cart", "brand": "Avitas", "strain": "GSC",
     "category": "cartridge", "subcategory": "hybrid", "size": "0.5g", "price": 22.0, "thc_percent": 79.5,
     "dominant_terpene": "Humulene", "stock_on_hand": 25, "why_this": "Half-gram, budget-friendly"},
    {"sku": "CT-DRUM-1G", "name": "Drum Roll Granddaddy 1g", "brand": "Drum Roll", "strain": "GDP",
     "category": "cartridge", "subcategory": "indica", "size": "1g", "price": 48.0, "thc_percent": 88.0,
     "dominant_terpene": "Linalool", "stock_on_hand": 9, "why_this": "Heavy indica cart for evenings"},
    {"sku": "CN-RSN-1G", "name": "Live Rosin 1g", "brand": "Ember", "strain": "Papaya",
     "category": "concentrate", "subcategory": "indica", "size": "1g", "price": 55.0, "thc_percent": 72.0,
     "dominant_terpene": "Myrcene", "stock_on_hand": 7, "why_this": "Solventless, full-spectrum"},
    {"sku": "CN-DOH-1G", "name": "DOH Compliant RSO 1g", "brand": "Medically Correct", "strain": None,
     "category": "concentrate", "subcategory": "indica", "size": "1g", "price": 42.0, "thc_percent": 65.0,
     "dominant_terpene": None, "stock_on_hand": 11, "why_this": "DOH-compliant medical option", "doh": True},
    {"sku": "PR-HALF-5", "name": "Half Ounce Pre-roll 5pk", "brand": "Roller Co", "strain": "Mixed",
     "category": "pre-roll", "subcategory": "hybrid", "size": "3.5g", "price": 25.0, "thc_percent": 21.0,
     "dominant_terpene": "Terpinolene", "stock_on_hand": 30, "why_this": "Five-pack, share-friendly"},
    {"sku": "PR-SINGLE-1", "name": "Single Pre-roll 1g", "brand": "Roller Co", "strain": "Blue Dream",
     "category": "pre-roll", "subcategory": "sativa", "size": "1g", "price": 6.0, "thc_percent": 20.5,
     "dominant_terpene": "Pinene", "stock_on_hand": 60, "why_this": "Cheapest way to try a strain"},
]


class FakeBudtender:
    """Stand-in for the budtender HTTP client that FILTERS like the real one.

    Slots actually matter here: a search that forgets to pass ``category`` gets everything, and a
    ``price_max`` that never reaches the client returns over-budget picks — both of which the
    thread assertions catch. Every call is recorded on ``.calls`` for inspection.
    """

    def __init__(self):
        self.catalog = [dict(row) for row in CATALOG]
        self.profile = {"has_history": False, "top_categories": [], "price_tier": ""}
        self.session_token = "sess-known-1"
        self.pairing = {"pairing": None, "strength": 0.0}
        self.calls: dict[str, list] = {}
        self.fail_search = False

    def _record(self, name: str, payload) -> None:
        self.calls.setdefault(name, []).append(payload)

    # -- product search ---------------------------------------------------
    def search(self, slots, *, limit=3, phone=None, session_token=None, exclude_skus=None, location=None):
        self._record("search", {"slots": dict(slots or {}), "limit": limit, "phone": phone,
                                "session_token": session_token, "exclude_skus": exclude_skus,
                                "location": location})
        if self.fail_search:
            return {"results": []}
        slots = slots or {}
        rows = self.catalog
        category = str(slots.get("category") or "").lower()
        if category:
            rows = [r for r in rows if r["category"] == category]
        if slots.get("subcategory"):
            rows = [r for r in rows if r.get("subcategory") == str(slots["subcategory"]).lower()]
        if slots.get("size"):
            rows = [r for r in rows if r.get("size") == slots["size"]]
        if slots.get("doh_only"):
            rows = [r for r in rows if r.get("doh")]
        if slots.get("price_max") is not None:
            rows = [r for r in rows if r["price"] <= float(slots["price_max"])]
        if slots.get("price_min") is not None:
            rows = [r for r in rows if r["price"] >= float(slots["price_min"])]
        for blocked in (slots.get("category_blocklist") or []):
            rows = [r for r in rows if r["category"] != str(blocked).lower()]
        for sku in (exclude_skus or []):
            rows = [r for r in rows if r["sku"] != sku]
        rows = sorted(rows, key=lambda r: r["price"])
        return {"results": [dict(r, rank=i + 1) for i, r in enumerate(rows[:limit])]}

    def check_sku(self, store, sku, *, category=None):
        self._record("check_sku", {"store": store, "sku": sku})
        row = next((r for r in self.catalog if r["sku"] == sku), None)
        if not row:
            return {"in_stock": False}
        return {"in_stock": True, "price_otd": round(row["price"] * 1.485, 2),
                "stock_on_hand": row["stock_on_hand"], "name": row["name"]}

    def pair_for_sku(self, store, anchor_sku, *, phone=None, session_token=None):
        self._record("pair_for_sku", {"store": store, "anchor": anchor_sku})
        return self.pairing

    # -- recognition ------------------------------------------------------
    def resume_by_phone(self, e164, *, location=None, current_session_token=None):
        self._record("resume_by_phone", {"phone": e164, "location": location})
        return {"profile_summary": self.profile, "session_token": self.session_token}

    # -- phone cart -------------------------------------------------------
    def phone_cart_upsert(self, payload):
        self._record("phone_cart_upsert", payload)
        return {"ok": True, "cart_id": "pc-1", "items": payload.get("items") or []}

    def phone_cart_release(self, payload):
        self._record("phone_cart_release", payload)
        return {"ok": True, "released": True}

    def phone_cart_claim(self, payload):
        self._record("phone_cart_claim", payload)
        return {"ok": True}

    def persist_session(self, *a, **kw):
        return {"ok": True}

    def health(self):
        return True


class Turn:
    """One agent reply, with the diagnostics a thread wants to assert on."""

    def __init__(self, said: str, result: dict):
        self.said = said
        self.raw = result

    @property
    def answer(self) -> str:
        return str(self.raw.get("answer") or "")

    @property
    def intent(self) -> str:
        return str(self.raw.get("intent") or "")

    @property
    def grounded(self) -> bool:
        return bool(self.raw.get("grounded"))

    @property
    def escalated(self) -> bool:
        return bool(self.raw.get("escalation_required"))

    @property
    def next_action(self) -> str:
        return str(self.raw.get("safe_next_action") or "")

    @property
    def sources(self) -> list:
        return self.raw.get("sources") or []

    @property
    def tools(self) -> list[str]:
        return [str(t.get("tool")) for t in (self.raw.get("tool_results") or [])]

    def _entry(self, tool: str) -> dict:
        for t in self.raw.get("tool_results") or []:
            if t.get("tool") == tool:
                return t
        return {}

    def args(self, tool: str) -> dict:
        """The slots the router DERIVED for that tool — where routing bugs show up."""
        return self._entry(tool).get("args") or {}

    def result(self, tool: str) -> dict:
        return self._entry(tool).get("result") or {}

    @property
    def picks(self) -> list[dict]:
        return self.result("suggest_products").get("picks") or []

    @property
    def pick_names(self) -> list[str]:
        return [p.get("name", "") for p in self.picks]

    def __repr__(self) -> str:
        return f"<Turn {self.intent!r} grounded={self.grounded} tools={self.tools}>"


class Conversation:
    """A caller with memory. ``say()`` carries history forward exactly like the website chat."""

    def __init__(self, store="yakima", phone="", slots=None):
        self.store = store
        self.phone = phone
        self.slots = slots or {}
        self.history: list[dict] = []
        self.turns: list[Turn] = []

    def say(self, message: str, **extra) -> Turn:
        from voice.chat import answer_text_chat

        payload = {
            "message": message,
            "store": self.store,
            "phone": self.phone,
            "history": list(self.history),
            "slots": dict(self.slots),
            "session_token": "convo-test",
        }
        payload.update(extra)
        turn = Turn(message, answer_text_chat(payload))
        self.history.append({"role": "user", "content": message})
        self.history.append({"role": "assistant", "content": turn.answer})
        self.turns.append(turn)
        return turn

    @property
    def transcript(self) -> str:
        return "\n".join(f"{m['role']}: {m['content']}" for m in self.history)


@pytest.fixture
def fake_bt(monkeypatch):
    """Patch the budtender client everywhere a module already imported it into its namespace."""
    fb = FakeBudtender()
    for module in (budtender_client, suggest, phone_cart, recognition):
        if hasattr(module, "budtender"):
            monkeypatch.setattr(module, "budtender", lambda: fb)
    return fb


@pytest.fixture
def seeded_kb(db):
    """The REAL production KB seed — grounded answers here are the ones a caller hears."""
    from kb.seed import seed_all

    return seed_all()


@pytest.fixture
def convo(seeded_kb, fake_bt):
    """Factory: ``c = convo(store="pullman", phone="+15095551234")``."""

    def _make(store="yakima", phone="", slots=None) -> Conversation:
        return Conversation(store=store, phone=phone, slots=slots)

    return _make
