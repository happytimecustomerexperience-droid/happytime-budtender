"""Thread 06 — Marcus, a wholesale rep at Cascade Crest, calls trying to reach the buyer: the
shared text brain has NO vendor route, so a B2B caller is shopped, apologized to, and never logged.
"""

from __future__ import annotations

import pytest

from voice import vendor_flow


@pytest.mark.django_db
def test_wholesale_rep_never_reaches_the_vendor_path(convo, fake_bt):
    """The full call. Six turns, each leaning on the last — and not one of them routes to the
    vendor leg (``notify_vendor_callback``) the phone squad's vendor member owns."""
    from crm.models import VendorCallback
    from voice.models import VoiceCall

    c = convo(store="yakima")

    # 1 — he opens the way a rep opens: name, company, and who he wants.
    t = c.say("hi this is Marcus with Cascade Crest Distribution, is your buyer available")
    assert t.tools == ["faq_lookup"], "the only routes are FAQ and product search — no vendor tool"
    assert t.intent == "greeting_other", "there is no vendor intent label to land on"
    assert t.grounded and t.sources, "it answers confidently anyway"
    # ...but the confident answer is about something else entirely: his question is never addressed.
    assert not any(
        word in t.answer.lower() for word in ("buyer", "purchasing", "receiving", "wholesale")
    ), f"answered a B2B question with an unrelated KB row: {t.answer!r}"

    # 2 — asked nothing useful, he pitches. "cartridge" is all the router hears.
    t = c.say("I'm calling about wholesale pricing on our new live resin cartridge line")
    assert t.intent == "product_suggestion", "a wholesale pitch is read as a retail ask"
    assert t.args("suggest_products") == {"category": "cartridge", "store": "yakima"}
    assert t.picks, "the rep gets shopped: retail inventory is searched on his behalf"
    assert t.next_action == "show_products"
    assert "out the door" in t.answer.lower(), "and quoted a consumer out-the-door price"
    assert "notify_vendor_callback" not in t.tools

    # 3 — he corrects the misread from turn 2. It does not help.
    t = c.say("no I'm not shopping, I need to send over a manifest and get a purchase order placed")
    assert t.tools == ["faq_lookup"], "the correction routes nowhere new"
    assert t.intent == "general_faq", "'order' matches the FAQ-first regex, not a vendor route"
    assert t.grounded
    assert "manifest" not in t.answer.lower(), (
        f"he asked about a manifest and got the online-order FAQ: {t.answer!r}"
    )

    # 4 — he escalates the human way, and trips the complaint detector on the word "manager".
    t = c.say("can you give me the purchasing manager's direct line then")
    assert t.intent == "conflict_resolution", "a vendor asking for the buyer is logged as a dispute"
    assert t.escalated is True
    assert t.next_action == "escalate"
    assert t.answer.startswith("I'm sorry that happened."), (
        f"the apology template for an upset customer is read to a sales rep: {t.answer!r}"
    )
    # Fixed 2026-08-07: the relevance gate only speaks a retrieved KB row mid-escalation when the
    # message itself matches _FAQ_FIRST_RE. "the purchasing manager's direct line" does not, so the
    # apology no longer gets an unrelated grounded row glued onto it — it falls back to the generic
    # escalation copy instead.
    assert not t.grounded, "no KB row matches this message, so nothing is glued onto the apology"
    assert t.tools == ["faq_lookup"], "escalation flags but dispatches no staff tool from text"

    # 5 — he tries the one phrasing the KB actually indexes. Turn 4's "manager" mention still keeps
    #     escalation live here (fixed 2026-08-07: _recent_escalation looks back over the caller's
    #     last 6 messages), and "delivery" matches _FAQ_FIRST_RE, so the relevance gate lets the
    #     grounded vendor row through — riding inside the apology instead of standing alone.
    t = c.say("look, I'm the driver too, I'm dropping off a delivery")
    vendor_sources = [s for s in t.sources if "vendor" in str(s.get("title", "")).lower()]
    assert vendor_sources, "the vendor StoreFact is only reachable by keyword luck, via faq_lookup"
    assert "call you back within one business day" in t.answer
    assert t.answer.startswith("I'm sorry that happened."), (
        "escalation is still live from turn 4, so the apology still prefixes the grounded answer"
    )
    assert "Vendor callback posture:" in t.answer, (
        f"the internal KB row label is still spoken verbatim to the caller: {t.answer!r}"
    )
    assert "notify_vendor_callback" not in t.tools, "it QUOTES the callback promise, never logs one"

    # 6 — he takes the promise at face value and asks for the callback. Escalation is still live
    #     from turn 4 (fixed 2026-08-07: 6-message lookback), and "invoice" matches no FAQ-first
    #     keyword, so this now escalates instead of quietly ending the call.
    t = c.say("perfect, just have someone call me back about the invoice then")
    assert t.tools == ["faq_lookup"]
    assert t.intent == "conflict_resolution"
    assert t.escalated is True
    assert t.next_action == "escalate", "the persisted escalation now actually hands off, rather than ending the call"

    # ── the whole call, in aggregate ────────────────────────────────────────────
    assert len(c.turns) == 6
    used = {tool for turn in c.turns for tool in turn.tools}
    assert used == {"faq_lookup", "suggest_products"}, (
        f"the text brain can only reach retail tools; vendor tools are unreachable: {used}"
    )
    assert VendorCallback.objects.count() == 0, "nothing durable survives a vendor call"
    assert VoiceCall.objects.count() == 0
    # The only budtender traffic on a B2B call: one retail search run for a wholesaler.
    assert len(fake_bt.calls.get("search", [])) == 1
    assert "resume_by_phone" not in fake_bt.calls

    # The classifier the text brain never consults would have folded his OWN words correctly —
    # the machinery exists (voice/vendor_flow.py), chat.py just has no path to it.
    assert vendor_flow.normalize_reason(c.turns[1].said) == vendor_flow.REASON_WHOLESALE
    assert vendor_flow.normalize_reason(c.turns[2].said) == vendor_flow.REASON_MANIFEST
    assert vendor_flow.normalize_reason(c.turns[4].said) == vendor_flow.REASON_DELIVERY
    assert vendor_flow.normalize_reason(c.turns[5].said) == vendor_flow.REASON_INVOICE


@pytest.mark.django_db
def test_rep_calls_pullman_back_and_the_kb_promises_a_callback_nobody_logs(convo):
    """Next morning, same rep, different store. Every turn is unambiguously B2B — and the
    grounded answers still come out of the retail knowledge base."""
    from crm.models import VendorCallback

    c = convo(store="pullman")

    t = c.say("morning, Marcus again from Cascade Crest, who handles purchasing over there")
    assert t.raw["store"] == "pullman", "store scoping still applies to a vendor call"
    assert t.intent == "greeting_other"
    assert "no delivery" in t.answer.lower(), (
        f"a delivery vendor is told we don't deliver (the retail pickup FAQ): {t.answer!r}"
    )

    t = c.say("I've got a Metrc transfer manifest I need to send before the truck rolls")
    assert t.intent == "greeting_other", "a manifest hand-off isn't even labelled a FAQ"
    assert t.grounded
    assert "manifest" not in t.answer.lower() and "metrc" not in t.answer.lower()
    assert vendor_flow.normalize_reason(t.said) == vendor_flow.REASON_MANIFEST

    t = c.say("what time does the store open tomorrow so I can time the drop")
    assert t.intent == "hours_location", "the label says hours..."
    assert not any("hours" in str(s.get("title", "")).lower() for s in t.sources), (
        f"...but no hours row was retrieved: {[s.get('title') for s in t.sources]}"
    )
    assert "9 AM" not in t.answer, f"so he never hears Pullman's opening time: {t.answer!r}"

    t = c.say("alright, can someone call me back about our wholesale account")
    assert t.tools == ["faq_lookup"]
    assert t.answer.startswith("pullman "), (
        f"the raw store slug from the KB chunk is spoken aloud: {t.answer!r}"
    )
    assert vendor_flow.callback_window_text("") in t.answer, "the KB promises the callback window"
    assert t.next_action == "answer", "and nothing acts on it"
    assert VendorCallback.objects.count() == 0, "the promised callback was never recorded"


@pytest.mark.django_db
def test_the_vendor_tool_works_the_text_brain_just_cannot_call_it(convo):
    """The gap is routing, not capability: the same intent that produces nothing through
    ``answer_text_chat`` produces a durable, alerted callback when the tool is dispatched."""
    from crm.models import VendorCallback
    from voice.models import Outcome, VoiceCall
    from voice.tools import dispatch

    c = convo(store="yakima")
    t = c.say("this is Marcus at Cascade Crest, I need a callback about our wholesale order")
    assert "notify_vendor_callback" not in t.tools
    assert VendorCallback.objects.count() == 0

    out = dispatch(
        "notify_vendor_callback",
        {
            "store": "yakima",
            "reason": vendor_flow.REASON_WHOLESALE,
            "summary": t.said,
            "caller_name": "Marcus (Cascade Crest)",
        },
        {"store": "yakima", "call_id": "vapi-thread-06", "caller_number": "+15095550142"},
    )
    assert out["logged"] is True
    assert out["callback_window"] == vendor_flow.callback_window_text("")
    assert "Yakima" in out["spoken"] and "one business day" in out["spoken"]
    assert "cost" not in out and "margin" not in out

    row = VendorCallback.objects.get(vapi_call_id="vapi-thread-06")
    assert row.reason == vendor_flow.REASON_WHOLESALE
    assert row.store == "yakima"
    assert VoiceCall.objects.get(call_id="vapi-thread-06").outcome == Outcome.VENDOR_CALLBACK
