"""Thread 05 — a caller on a blocked line: no caller-ID reaches budtender, yet the call is fully served.

The mirror image of the recognized caller. With no usable number the identity handshake
(``resume_by_phone``) never fires, ``search`` is called with ``phone=None`` /
``session_token=None`` (budtender's W_ANON margin-first ranking), ``profile_summary.has_history``
stays False so nothing is inferred from a past basket — and hours, products, budgets and the
return policy all still land. One turn hands the router a too-short number to prove
``chat.py::_phone_hint`` REJECTS junk instead of forwarding it, and a second conversation walks the
same caller from junk digits to a real number so the accept path proves the reject path is real.
"""

from __future__ import annotations

import pytest

# Catalog names from the harness fixture — a blocked caller must never be handed a "pick" for
# a question that never routed to the budtender.
_CATALOG_TELLS = ("Blueberry OG", "Jetty", "Wyld", "Avitas", "Gorilla Glue")


def _searches(fake_bt) -> list[dict]:
    return fake_bt.calls.get("search", [])


@pytest.mark.django_db
def test_blocked_caller_is_served_without_ever_identifying_them(convo, fake_bt):
    """Seven turns on an anonymous line: full service, zero identity, zero junk forwarded."""
    c = convo(store="yakima", phone="")

    # ── 1. Opener: a plain store question. Anonymous or not, hours come from the KB. ──
    t = c.say("hi there — how late are you open tonight?")
    assert t.intent == "hours_location"
    assert t.grounded, "hours must come from the KB, never invented"
    assert t.sources, "a grounded answer must cite the row it came from"
    assert t.tools == ["faq_lookup"]
    assert "11:30" in t.answer, "the Yakima hours row is what a known caller hears too"
    assert "resume_by_phone" not in fake_bt.calls, "no number ⇒ no identity handshake at all"
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": ""}

    # ── 2. The product ask — the margin-first half of the ADR-005 switch. ──
    t = c.say("good to know. I'm after a cartridge that helps me relax, under $40")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["effect_desired"] == "relaxed"
    assert args["price_max"] == 40.0
    assert t.picks, "an anonymous caller still gets real in-stock picks"
    assert t.next_action == "show_products"

    call = _searches(fake_bt)[-1]
    assert call["phone"] is None, "NO caller number may reach budtender — that is W_ANON margin-first"
    assert call["session_token"] is None, (
        "recognition nulls the inbound session_token too, so identity can't ride in the back door "
        "(the payload carried the harness's own per-conversation session_token, not a caller identity)"
    )
    assert "resume_by_phone" not in fake_bt.calls
    for pick in t.picks:
        assert "cost" not in pick and "margin" not in pick

    # The old GAP here (price_max filtering budtender's pre-tax price while the agent spoke an
    # OTD-uplifted price) is gone now that otd() is identity-with-rounding: the menu price IS the
    # OTD price, so a "$40" budget is quoted back at or under $40, exactly as it should be.
    assert max(p["price_otd"] for p in t.picks) <= 40.0
    assert "35 dollars" in [p["price_spoken"] for p in t.picks]

    # ── 3. The caller reacts to that price and asks for a second category. ──
    # GAP (see findings): the throwaway "good deal" puts an ``_FAQ_FIRST_RE`` word in the sentence,
    # so ``_prefers_products`` suppresses the product route and the flower ask is dropped on the
    # floor — no search runs. "good deal" also reads as chat.py's specials topic, so the answer now
    # comes out of a topic-constrained retrieval for real (retrieval-precision follow-up): the
    # label and the SPOKEN content finally agree — both ``specials`` — instead of the old
    # accidental mismatch where an unconstrained ranker happened to surface the eighth glossary
    # entry under a wrong ``specials`` label. The underlying mislabel (chat.py reading "good deal"
    # as a specials ask instead of a flower ask) is still the real bug — out of scope here.
    t = c.say("that's a good deal — how about an eighth of indica flower too?")
    assert t.intent == "specials"
    assert "suggest_products" not in t.tools
    assert len(_searches(fake_bt)) == 1, "the flower ask never reached budtender"
    # UPDATED 2026-09-01: deals carry a validity window now (StoreFact.valid_from/valid_to)
    # and the only seeded set is July's, whose window has closed, so the specials answer is
    # the honest "nothing posted right now" — ungrounded by design (no KB row asserts an
    # absence). Post a current deal in the dashboard and this grounds on it again.
    assert t.grounded is True or "specials posted" in t.answer
    assert "% off" in t.answer or "specials posted" in t.answer, (
        "the label and the spoken content agree: either the deals that are running, or a "
        "plain statement that none are"
    )
    assert "3.5 g" not in t.answer, "no longer the accidental eighth-glossary mismatch"

    # ── 4. Open-ended ask: with has_history False there is no taste to lean on. ──
    # A recognized caller's ``top_categories`` would turn this into a product_suggestion
    # (see the second test); a blocked caller gets no such inference.
    t = c.say("what would you suggest for someone like me?")
    assert "suggest_products" not in t.tools, "nothing to personalize from ⇒ no product route"
    assert t.intent == "greeting_other"
    assert len(_searches(fake_bt)) == 1, "no second budtender search was triggered"
    for tell in _CATALOG_TELLS:
        assert tell not in t.answer, "no picks may be voiced on a turn that never reached budtender"
    # FIXED (retrieval-precision follow-up): nothing in the KB answers "what would you suggest" —
    # the old unconstrained ranker used to confidently ground an unrelated row (WA age/ID limits)
    # anyway. The relevance floor now correctly declines instead of guessing.
    assert t.grounded is False

    # ── 5. The caller reads out a mangled number — the parser must drop it, not forward it. ──
    t = c.say(
        "here's my number in case we get cut off — 555-1234. and do you have gummies under $20?",
        phone="555-1234",
    )
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "edible"
    assert args["price_max"] == 20.0
    assert t.picks

    call = _searches(fake_bt)[-1]
    assert call["phone"] is None, "a 7-digit fragment is not a phone number and must not be forwarded"
    assert call["session_token"] is None
    assert "resume_by_phone" not in fake_bt.calls, "junk digits must never trigger a profile lookup"
    assert t.raw["contact_hint"]["customer_phone"] == "", "junk must not leak into the staff contact hint"

    # ── 6. Policy question — the grounded/sourced path is unchanged for an anonymous caller. ──
    t = c.say("before I forget — what's your return policy?")
    assert t.intent == "return_policy"
    assert t.grounded and t.sources
    assert t.escalated is False
    assert t.tools == ["faq_lookup"]
    assert "All sales are final" in t.answer
    assert "WAC 314-55-079" in t.answer

    # ── 7. Sign-off. ──
    t = c.say("perfect, thanks so much")
    assert t.intent == "greeting_other"
    assert t.tools == ["faq_lookup"]
    # FIXED (retrieval-precision follow-up): a bare thank-you used to be answered with a
    # confident, unrelated KB row. The relevance floor now declines instead of guessing.
    assert t.grounded is False

    # ── whole-call invariants ──
    assert len(c.turns) == 7
    assert c.transcript.count("user:") == 7
    assert "resume_by_phone" not in fake_bt.calls, "not one identity lookup in the entire call"
    assert len(_searches(fake_bt)) == 2
    assert all(s["phone"] is None for s in _searches(fake_bt))
    assert all(s["session_token"] is None for s in _searches(fake_bt))
    assert all(s["location"] == "yakima" for s in _searches(fake_bt))


@pytest.mark.django_db
def test_junk_digits_are_rejected_until_a_real_number_lands(convo, fake_bt):
    """The same caller re-reads their number twice badly, then correctly — the parser is the gate."""
    # This caller HAS shopped before; the only thing standing between them and taste-first
    # ranking is whether _phone_hint accepts what they read out.
    fake_bt.profile = {"has_history": True, "top_categories": ["flower"], "price_tier": "mid"}
    c = convo(store="pullman", phone="")

    # ── 1. Six digits — a fragment. ──
    t = c.say("hey, can you recommend something for me? my number's 509-555", phone="509-555")
    assert "suggest_products" not in t.tools
    assert t.intent == "greeting_other"
    assert t.raw["contact_hint"]["customer_phone"] == ""
    assert fake_bt.calls == {}, "a fragment must not reach budtender by any path"

    # ── 2. Seven digits — still short, still refused. ──
    t = c.say("sorry, that got cut off — 5095551", phone="5095551")
    assert "suggest_products" not in t.tools
    assert t.intent == "greeting_other"
    assert fake_bt.calls == {}, "still nothing forwarded — the parser hasn't accepted anything yet"

    # ── 3. Ten digits with punctuation — accepted, normalized, and the caller becomes known. ──
    ask = "okay try this — (509) 555-0142. so what would you recommend?"
    assert "flower" not in ask.lower(), "the category must come from the profile, not the words"
    t = c.say(ask, phone="(509) 555-0142")
    assert fake_bt.calls["resume_by_phone"] == [{"phone": "+15095550142", "location": "pullman"}]
    assert t.intent == "product_suggestion", "the same open-ended ask that fell through while junk"
    assert t.args("suggest_products")["category"] == "flower", "inferred from profile.top_categories"
    assert t.picks

    call = _searches(fake_bt)[-1]
    assert call["phone"] == "+15095550142", "a recognized caller's number IS forwarded (W_KNOWN)"
    assert call["session_token"] == "sess-known-1"

    # ── 4. Budget follow-up on the now-recognized line. ──
    t = c.say("nice — anything like that under $30?", phone="(509) 555-0142")
    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert args["price_max"] == 30.0
    assert t.picks
    assert _searches(fake_bt)[-1]["phone"] == "+15095550142"
    # Same fix as turn 2: otd() is identity now, so the pick that survives a "$30" filter is
    # quoted back at or under $30.
    assert t.picks[0]["price_otd"] <= 30.0

    assert len(c.turns) == 4
    assert len(fake_bt.calls["resume_by_phone"]) == 2, "only the two turns with a real number"
    assert len(_searches(fake_bt)) == 2
