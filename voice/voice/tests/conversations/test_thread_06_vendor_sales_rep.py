"""Thread 06 — Marcus, a wholesale rep at Cascade Crest, calls trying to reach the buyer.

FIXED 2026-08-10: the shared text brain had NO vendor route at all — a B2B caller was shopped,
apologized to, and never logged. ``answer_text_chat`` now detects a vendor/sales-rep/delivery-
driver opener and dispatches ``notify_vendor_callback`` itself (voice/chat.py's vendor gate),
subject to ONE rule pinned by turns 4-6 below: vendor detection LOSES to escalation. Marcus trips
the escalation regex at turn 4 (the word "manager") and stays escalated for the rest of the call
(the existing 6-message carry window) — exactly like a hostile caller who happens to mention
"delivery" is not treated as a vendor, a vendor caught in an escalated turn is not silently routed
around a human either.
"""

from __future__ import annotations

import pytest

from voice import vendor_flow


@pytest.mark.django_db
def test_wholesale_rep_now_routes_to_the_vendor_path(convo, fake_bt):
    """The full call. Six turns — the first three land cleanly on the vendor tool; the last three
    stay escalated (turn 4's "manager" trips escalation, which outranks vendor detection, and the
    dispute carries for the rest of the call, same as any other dispute would)."""
    from crm.models import VendorCallback
    from voice.models import Outcome, VoiceCall

    c = convo(store="yakima")

    # 1 — he opens the way a rep opens: name, company, and who he wants. Vendor-detected on
    # "your buyer available" — no retail/FAQ tool ever gets to shop or apologize at him.
    t = c.say("hi this is Marcus with Cascade Crest Distribution, is your buyer available")
    assert t.intent == "vendor_callback"
    assert t.tools == ["faq_lookup", "notify_vendor_callback"], (
        "faq_lookup still runs (unconditional, unchanged) but now so does the vendor tool"
    )
    assert t.grounded is False
    assert "call you back" in t.answer and "one business day" in t.answer
    assert t.next_action == "answer"

    # 2 — the wholesale pitch. FIXED: this used to be read as a retail ask ("cartridge" in his
    # sentence) and he got shopped. It now routes to vendor BEFORE the product branch ever runs —
    # no suggest_products call happens at all.
    t = c.say("I'm calling about wholesale pricing on our new live resin cartridge line")
    assert t.intent == "vendor_callback"
    assert "suggest_products" not in t.tools, "no longer shopped as a retail cartridge ask"
    assert t.args("notify_vendor_callback")["reason"] == vendor_flow.REASON_WHOLESALE

    # 3 — the manifest/PO correction. Also vendor-detected now (was: routed nowhere).
    t = c.say("no I'm not shopping, I need to send over a manifest and get a purchase order placed")
    assert t.intent == "vendor_callback"
    assert t.args("notify_vendor_callback")["reason"] == vendor_flow.REASON_MANIFEST

    # 4 — he escalates the human way, and trips the complaint detector on the word "manager".
    # UNCHANGED (by design — see module docstring): vendor detection loses to escalation, so this
    # still reads as a dispute, not a vendor ask, even though "purchasing manager" is itself
    # vendor-shaped language.
    t = c.say("can you give me the purchasing manager's direct line then")
    assert t.intent == "conflict_resolution", "escalation still outranks vendor detection"
    assert t.escalated is True
    assert t.next_action == "escalate"
    assert t.answer.startswith("I'm sorry that happened."), (
        f"the apology template for an upset customer is read to a sales rep: {t.answer!r}"
    )
    assert not t.grounded, "no KB row matches this message, so nothing is glued onto the apology"
    assert t.tools == ["faq_lookup"], "escalation wins, so the vendor tool never fires here"

    # 5 — UNCHANGED: turn 4's "manager" mention keeps escalation live (the existing 6-message
    # carry window), so even this unambiguous delivery-driver line stays on the escalation path —
    # the vendor gate never gets a chance to fire while escalation is carried.
    t = c.say("look, I'm the driver too, I'm dropping off a delivery")
    vendor_sources = [s for s in t.sources if "vendor" in str(s.get("title", "")).lower()]
    assert vendor_sources, "the vendor StoreFact is only reachable by keyword luck, via faq_lookup"
    assert "call you back within one business day" in t.answer
    assert t.answer.startswith("I'm sorry that happened.")
    assert "Vendor callback posture:" in t.answer, (
        f"the internal KB row label is still spoken verbatim to the caller: {t.answer!r}"
    )
    assert "notify_vendor_callback" not in t.tools, (
        "escalation still wins here — it QUOTES the callback promise, never logs one on this turn"
    )

    # 6 — same carried-escalation story.
    t = c.say("perfect, just have someone call me back about the invoice then")
    assert t.tools == ["faq_lookup"]
    assert t.intent == "conflict_resolution"
    assert t.escalated is True
    assert t.next_action == "escalate"

    # ── the whole call, in aggregate ────────────────────────────────────────────
    assert len(c.turns) == 6
    used = {tool for turn in c.turns for tool in turn.tools}
    assert used == {"faq_lookup", "notify_vendor_callback"}, (
        f"the text brain now reaches the vendor tool and never touches retail tools at all: {used}"
    )
    # FIXED: a callback IS now durably logged (created on turn 1; ``notify_vendor_callback`` is
    # idempotent per call_id, so turns 2-3's repeat vendor-sounding language confirms the SAME row
    # rather than creating duplicates or re-firing the staff alert).
    assert VendorCallback.objects.count() == 1
    row = VendorCallback.objects.get()
    assert row.store == "yakima"
    assert row.reason == vendor_flow.REASON_OTHER, (
        "turn 1 named no specific reason, and idempotent get_or_create keeps that first reason"
    )
    call = VoiceCall.objects.get(call_id=c.session_token)
    assert call.outcome == Outcome.VENDOR_CALLBACK
    assert call.turns.count() == 12, "6 user + 6 assistant turns, all under this session's own token"
    # No retail traffic AT ALL on this B2B call now — the old bug's "one retail search run for a
    # wholesaler" is gone along with it.
    assert fake_bt.calls == {}, f"a vendor call should never touch budtender: {fake_bt.calls}"

    # The classifier the text brain now actually reaches: vendor_flow.normalize_reason folds each
    # turn's own words into the same enum the durable record carries.
    assert vendor_flow.normalize_reason(c.turns[1].said) == vendor_flow.REASON_WHOLESALE
    assert vendor_flow.normalize_reason(c.turns[2].said) == vendor_flow.REASON_MANIFEST
    assert vendor_flow.normalize_reason(c.turns[4].said) == vendor_flow.REASON_DELIVERY
    assert vendor_flow.normalize_reason(c.turns[5].said) == vendor_flow.REASON_INVOICE


@pytest.mark.django_db
def test_rep_calls_pullman_back_and_the_callback_is_now_logged(convo):
    """Next morning, same rep, different store. Every turn is unambiguously B2B, and now every one
    of them (except the store-hours aside, which names no vendor language at all) routes straight
    to the vendor tool instead of the retail knowledge base."""
    from crm.models import VendorCallback

    c = convo(store="pullman")

    # FIXED: "who handles purchasing" now vendor-routes instead of grounding on an unrelated row.
    t = c.say("morning, Marcus again from Cascade Crest, who handles purchasing over there")
    assert t.raw["store"] == "pullman", "store scoping still applies to a vendor call"
    assert t.intent == "vendor_callback"
    assert t.grounded is False

    # FIXED: "manifest" now vendor-routes too.
    t = c.say("I've got a Metrc transfer manifest I need to send before the truck rolls")
    assert t.intent == "vendor_callback"
    assert t.args("notify_vendor_callback")["reason"] == vendor_flow.REASON_MANIFEST
    assert vendor_flow.normalize_reason(t.said) == vendor_flow.REASON_MANIFEST

    # UNCHANGED — no vendor language in this line at all, so it stays on the ordinary (still
    # store-blind, still a separate pre-existing retrieval bug, out of scope here) hours path.
    t = c.say("what time does the store open tomorrow so I can time the drop")
    assert t.intent == "hours_location", "the label says hours..."
    assert not any("hours" in str(s.get("title", "")).lower() for s in t.sources), (
        f"...but no hours row was retrieved: {[s.get('title') for s in t.sources]}"
    )
    assert "9 AM" not in t.answer, f"so he never hears Pullman's opening time: {t.answer!r}"

    # FIXED: this is the turn the original bug was named for — the KB used to just recite the
    # callback-window promise with nothing durable behind it. It now actually logs one.
    t = c.say("alright, can someone call me back about our wholesale account")
    assert t.tools == ["faq_lookup", "notify_vendor_callback"]
    assert t.intent == "vendor_callback"
    assert t.args("notify_vendor_callback")["reason"] == vendor_flow.REASON_WHOLESALE
    assert t.next_action == "answer"
    # The callback was ALREADY logged on turn 1 (idempotent per session); this turn confirms the
    # same durable row rather than creating a second one.
    assert VendorCallback.objects.count() == 1
    row = VendorCallback.objects.get()
    assert row.store == "pullman"


@pytest.mark.django_db
def test_the_vendor_tool_now_gets_called_directly_by_the_text_brain(convo):
    """FIXED: the gap used to be routing, not capability — the tool worked fine when dispatched by
    hand, ``answer_text_chat`` just never called it. Now a single vendor-shaped turn through the
    shared text brain produces the same durable, alerted callback the direct-dispatch path always
    could."""
    from crm.models import VendorCallback
    from voice.models import Outcome, VoiceCall

    c = convo(store="yakima")
    t = c.say("this is Marcus at Cascade Crest, I need a callback about our wholesale order")
    assert "notify_vendor_callback" in t.tools, "chat.py now calls the vendor tool itself"
    assert t.intent == "vendor_callback"
    assert "cost" not in t.answer and "margin" not in t.answer

    row = VendorCallback.objects.get(vapi_call_id=c.session_token)
    assert row.reason == vendor_flow.REASON_WHOLESALE
    assert row.store == "yakima"
    assert row.alerted is True
    assert VoiceCall.objects.get(call_id=c.session_token).outcome == Outcome.VENDOR_CALLBACK

    result = t.result("notify_vendor_callback")
    assert result["logged"] is True
    assert result["callback_window"] == vendor_flow.callback_window_text("")
    assert "Yakima" in result["spoken"] and "one business day" in result["spoken"]
    assert "cost" not in result and "margin" not in result
