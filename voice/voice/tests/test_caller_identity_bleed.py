"""PINNING TEST — do not fix, just document the truth.

``chat.answer_text_chat`` takes ``history`` straight from the request body (``data.get("history")``,
chat.py ~line 371) with no linkage to the ``ctx``/phone the SAME request carries. ``_carried_category``
(~line 281), ``_recent_escalation`` (~line 232), and ``_profile_top_category`` (~line 203) read that
``history`` array (or, for the third, ``ctx["profile_summary"]`` from ``recognition.resolve_caller``)
with no caller-identity check binding it to the phone in ``ctx``.

Finding: if a caller (any client of the /chat endpoint) submits a ``history`` array that did not
originate from their own prior turns — e.g. copied, replayed, or simply mis-threaded by a buggy
client — the CURRENT caller's escalation state and product-category context bleed in, regardless
of what phone number is attached to the current turn. There is no server-side session store keyed
by phone/call_id that ``history`` is checked against; the array is trusted as-is.

This test pins that behaviour with two different phone numbers sharing one (attacker/mis-threaded)
history array. It does NOT change chat.py.
"""

from __future__ import annotations

import pytest

from voice import chat


def _ungrounded_dispatch(tool, args, ctx):
    if tool == "faq_lookup":
        return {"grounded": False, "fallback": "can't confirm"}
    if tool == "suggest_products":
        return {"picks": [], "spoken_summary": ""}
    return {}


@pytest.fixture(autouse=True)
def _stub_tools_and_recognition(monkeypatch):
    monkeypatch.setattr(chat, "dispatch", _ungrounded_dispatch)
    # Recognition resolves per-phone but carries NO history of its own — isolates the bleed to the
    # client-supplied `history` array, not to any server-side profile mixing.
    monkeypatch.setattr(
        chat.recognition,
        "resolve_caller",
        lambda number, ctx: {
            "known": True,
            "profile_summary": {"has_history": False, "top_categories": [], "price_tier": ""},
        },
    )


# A dispute from whoever generated this history array — no phone of their own recorded in it,
# because `history` is just role/content pairs, never phone-stamped.
_FOREIGN_DISPUTE_HISTORY = [
    {"role": "user", "content": "my vape cart is defective and won't fire, I want a refund"},
    {"role": "assistant", "content": "I'm sorry that happened, let me get the store team involved."},
]

_FOREIGN_CATEGORY_HISTORY = [
    {"role": "user", "content": "do you have any flower in stock"},
    {"role": "assistant", "content": "Sure, here are a few options."},
]


def test_escalation_state_bleeds_from_unrelated_history_regardless_of_phone():
    """Caller B (a distinct phone, no escalation wording of their own, just says "hey there")
    inherits caller-A-shaped conflict_resolution state purely because the request carried A's
    history array. `_recent_escalation` never checks whose history this is."""
    out = chat.answer_text_chat(
        {
            "message": "hey there",
            "store": "yakima",
            "phone": "5095550001",
            "history": _FOREIGN_DISPUTE_HISTORY,
        }
    )
    # TRUTH: escalation bled in from a history array that was never tied to this phone.
    assert out["intent"] == "conflict_resolution"
    assert out["escalation_required"] is True
    assert out["escalation_flag"] is True

    # A second, different phone with the SAME foreign history gets the identical bleed — proving
    # the phone in ctx plays no role at all in whether history is trusted.
    out2 = chat.answer_text_chat(
        {
            "message": "hey there",
            "store": "yakima",
            "phone": "5095559999",
            "history": _FOREIGN_DISPUTE_HISTORY,
        }
    )
    assert out2["intent"] == "conflict_resolution"
    assert out2["escalation_required"] is True


def test_product_category_bleeds_from_unrelated_history_via_refinement_carry():
    """Caller B never mentions a category; they just say a bare refinement ("keep it under 40
    though"). `_carried_category` pulls "flower" out of a history array that belongs to a
    different phone entirely."""
    out = chat.answer_text_chat(
        {
            "message": "keep it under 40 though",
            "store": "yakima",
            "phone": "5095550002",
            "history": _FOREIGN_CATEGORY_HISTORY,
        }
    )
    suggest_calls = [tr for tr in out["tool_results"] if tr["tool"] == "suggest_products"]
    assert suggest_calls, "expected a suggest_products dispatch carrying the bled category"
    # TRUTH: "flower" came from a different caller's history, not from this caller's own turn.
    assert suggest_calls[0]["args"]["category"] == "flower"
