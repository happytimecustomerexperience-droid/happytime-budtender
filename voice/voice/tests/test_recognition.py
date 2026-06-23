"""voice/recognition.py — returning-caller recognition + PII discipline (11-P1 §3.4; AC E).

budtender ``resume_by_phone`` is MOCKED. Offline, no network. Asserts: peppered + deterministic
hash; HIT → known/taste-first (session_token carried); MISS → anonymous/margin-first; blocked
number → skip; the raw number is never persisted (only the peppered hash).
"""

from __future__ import annotations

import pytest

from crm.models import phone_hash
from voice import recognition


class FakeClient:
    def __init__(self, summary):
        self.summary = summary
        self.calls: list[dict] = []

    def resume_by_phone(self, phone_e164, *, location=None, current_session_token=None):
        self.calls.append({"phone": phone_e164, "location": location})
        return {
            "resumed": bool(self.summary.get("has_history")),
            "session_token": "s-known"
            if self.summary.get("has_history")
            else current_session_token,
            "profile_summary": self.summary,
        }


# ── E1. peppered + deterministic hash ───────────────────────────────────────────
def test_phone_hash_is_peppered_and_deterministic(settings):
    settings.PHONE_HASH_PEPPER = "pepper-A"
    h1 = recognition.phone_hash("+1 (509) 555-1234")
    h2 = recognition.phone_hash("+15095551234")
    assert h1 == h2 and len(h1) == 64  # format-insensitive, deterministic
    assert h1 == phone_hash("+15095551234")  # re-export matches crm.models


def test_phone_hash_changes_with_pepper(settings):
    settings.PHONE_HASH_PEPPER = "pepper-A"
    a = recognition.phone_hash("+15095551234")
    settings.PHONE_HASH_PEPPER = "pepper-B"
    b = recognition.phone_hash("+15095551234")
    assert a != b


def test_normalize_e164():
    assert recognition.normalize_e164("5095551234") == "+15095551234"
    assert recognition.normalize_e164("+1 509 555 1234") == "+15095551234"
    assert recognition.normalize_e164("15095551234") == "+15095551234"
    assert recognition.normalize_e164("") == ""


# ── E2. HIT → known/taste-first ; MISS → anonymous/margin-first ─────────────────
def test_recognition_hit_sets_known_and_session():
    client = FakeClient({"has_history": True, "top_categories": ["flower"], "price_tier": "mid"})
    ctx = {"call_id": "c1", "store": "yakima"}
    recognition.resolve_caller("+15095551234", ctx, client=client)
    assert ctx["known"] is True
    assert ctx["session_token"] == "s-known"  # carried into W_KNOWN
    assert ctx["_caller_phone"] == "+15095551234"  # transient identity for the search call
    assert ctx["profile_summary"]["top_categories"] == ["flower"]


def test_recognition_miss_is_anonymous():
    client = FakeClient({"has_history": False, "top_categories": [], "price_tier": ""})
    ctx = {"call_id": "c2", "store": "yakima"}
    recognition.resolve_caller("+15095551234", ctx, client=client)
    assert ctx["known"] is False
    assert ctx["session_token"] is None  # → W_ANON (margin-first)
    assert ctx["_caller_phone"] is None


# ── E3. blocked/absent number → handshake skipped, margin-first, no error ───────
def test_blocked_number_skips_handshake():
    client = FakeClient({"has_history": True})  # would HIT if called
    ctx = {"call_id": "c3", "store": "yakima"}
    recognition.resolve_caller("", ctx, client=client)
    assert ctx["known"] is False
    assert client.calls == []  # never called budtender for a blocked number


# ── E3 / PII: only the peppered hash is stamped (never the raw number) ──────────
def test_ctx_carries_hash_not_raw_number():
    client = FakeClient({"has_history": False})
    ctx = {"call_id": "c4", "store": "yakima"}
    recognition.resolve_caller("+15095551234", ctx, client=client)
    assert ctx["caller_phone_hash"] and len(ctx["caller_phone_hash"]) == 64
    assert ctx["caller_phone_hash"] != "+15095551234"


@pytest.mark.django_db
def test_voicecall_persists_only_hash_never_raw_number(client_settings=None):
    """After a suggest turn the VoiceCall row stores ONLY the peppered hash — the raw caller
    number appears in no persisted field (ADR-006 PII discipline, AC E3)."""
    from voice import budtender_client
    from voice.models import VoiceCall
    from voice.tools import suggest

    raw = "+15095559999"
    fake_summary = {"has_history": False, "top_categories": [], "price_tier": ""}

    class _BT:
        def resume_by_phone(self, phone_e164, *, location=None, current_session_token=None):
            return {"session_token": None, "profile_summary": fake_summary}

        def search(self, slots, **kw):
            return {
                "results": [
                    {
                        "sku": "S1",
                        "name": "X",
                        "price": 10.0,
                        "why_this": "ok",
                        "brand": "B",
                        "strain": None,
                        "thc_percent": None,
                        "rank": 1,
                    }
                ]
            }

    import voice.recognition as recog

    bt = _BT()
    # recognition uses the default client; patch the factory both places.
    orig = budtender_client.budtender
    budtender_client.budtender = lambda: bt
    suggest_orig = suggest.budtender
    suggest.budtender = lambda: bt
    try:
        ctx = {"call_id": "call_pii_1", "store": "yakima", "caller_number": raw}
        # resolve_caller (default client path) then suggest stamps the row.
        recog.resolve_caller(raw, ctx, client=bt)
        suggest.handle_suggest_products({"store": "yakima", "category": "flower"}, ctx)
    finally:
        budtender_client.budtender = orig
        suggest.budtender = suggest_orig

    vc = VoiceCall.objects.get(call_id="call_pii_1")
    # The raw number must not appear in any stored text field.
    blob = vc.caller_phone_hash + (vc.transcript or "") + str(vc.suggested_skus)
    assert raw not in blob
    assert len(vc.caller_phone_hash) == 64
