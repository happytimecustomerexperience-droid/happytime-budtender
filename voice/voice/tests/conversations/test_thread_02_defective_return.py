"""Thread 02 — Dana, whose two-day-old cartridge won't fire: the WAC defective-return path,
what defective/refund language does to the route, and the handoff to staff.
"""

from __future__ import annotations

import pytest

from kb.seed import FAQ_ROWS, RETURN_POLICY_BODY

# The KB's own words for the defective path. If these appear in the answer verbatim, nothing
# about the remedy was composed by the agent (Numbers-Guard: the row speaks, not the LLM).
RETURNS_ROW_ANSWER = next(r["answer"] for r in FAQ_ROWS if r["key"] == "returns")

# Phrasings that would commit the store to giving money back. The KB says the remedy is an
# EXCHANGE and that cash-back refunds are not given, so none of these may ever be spoken.
_REFUND_PROMISES = (
    "we'll refund",
    "we will refund",
    "refund you",
    "you'll get a refund",
    "you will get a refund",
    "you'll get your money back",
    "get your money back",
    "money back guarantee",
    "full refund",
    "i can refund",
    "we can refund",
)


def _promises_refund(answer: str) -> str:
    """The offending phrase, or "" — used so a failure names what the agent promised."""
    low = answer.lower()
    return next((phrase for phrase in _REFUND_PROMISES if phrase in low), "")


@pytest.mark.django_db
def test_defective_cartridge_return_thread(convo, fake_bt):
    """Dana calls Yakima: dead cart → policy → what to bring → get me a person → back to shopping."""
    c = convo(store="yakima", phone="+15095550142")

    # 1 ─ the complaint. "won't fire" is defect language: the turn must escalate, and the
    # policy it quotes has to come out of the KB, not out of the model.
    t = c.say("hi, I picked up a cartridge at your Yakima store two days ago and it won't fire at all")
    assert t.intent == "conflict_resolution", "defect language outranks the FAQ topic label"
    assert t.escalated is True
    assert t.next_action == "escalate"
    assert t.grounded and t.sources, "the WAC remedy must be quoted from a real KB row"
    assert RETURNS_ROW_ANSWER in t.answer, "the returns row is spoken verbatim, not paraphrased"
    assert t.answer.startswith("I'm sorry that happened.")
    assert not _promises_refund(t.answer), _promises_refund(t.answer)
    # A caller reporting a dead cart is NOT a shopping lead: no product search fires even though
    # the word "cartridge" is right there in the sentence.
    assert t.tools == ["faq_lookup"]
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": "+15095550142"}

    # 2 ─ the question the whole call is about. "refund" is itself an escalation trigger, so the
    # dispute label wins over return_policy — and the answer still offers only an exchange.
    t = c.say("so can I get a refund, or is it just an exchange?")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True and t.next_action == "escalate"
    assert t.grounded and t.sources, "a returns/refund question may not be answered source-free"
    assert "exchanged with no time limit" in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)
    assert "Return policy" in [s["title"] for s in t.sources]

    # 3 ─ the natural follow-up. It is unmistakably about the return she just asked about.
    # FIXED 2026-08-07 (was FINDING 1): _recent_escalation now looks back over her last few
    # turns, so this correctly stays a dispute instead of resetting to small talk.
    t = c.say("do I need the original packaging and the receipt?")
    assert t.intent == "conflict_resolution", "fixed 2026-08-07: escalation now persists across turns"
    assert t.escalated is True and t.next_action == "escalate"
    # REGRESSION (found 2026-08-07, left failing on purpose): this is a genuinely on-topic,
    # KB-covered follow-up — the same query was answered correctly before any of the six fixes
    # (with the wrong intent label, but the right content). The new relevance gate in chat.py
    # (_FAQ_FIRST_RE) only lets a grounded row be spoken on an escalation turn when the message
    # itself contains one of its trigger words (return/refund/policy/defective/...); "packaging"
    # and "receipt" aren't in that list, so a real, correct answer that used to reach her is now
    # silently swapped for the generic "I can't confirm that" fallback. That's a fix-#6 gap, not
    # a test bug — flag for review.
    assert t.grounded and t.sources, "the WAC remedy must be quoted from a real KB row"
    assert t.sources[0]["kind"] == "policy"
    assert "original packaging" in t.answer and "receipt" in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 4 ─ she asks for a person. The handoff itself is right: flagged, routed, contact captured.
    t = c.say("I'd really like a staff member to call me back about it")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True and t.next_action == "escalate"
    assert t.tools == ["faq_lookup"]
    # FIXED 2026-08-07 (was FINDING 3): the relevance gate means the off-topic EVENTS row is no
    # longer read out for a message that doesn't ask about anything the KB covers — no more
    # promo blurb bracketing the apology.
    assert t.grounded is False and t.sources == []
    # REGRESSION (found 2026-08-07, left failing on purpose): the callback number used to ride
    # along in the handoff line (see turn 1, which goes through _staff_followup_hint(store,
    # phone)). This turn now falls into the *un-grounded* escalation branch (chat.py
    # ~L366-384), which is a fixed string that never calls _staff_followup_hint — so a phone
    # number we already have (see contact_hint below) silently drops out of what she's actually
    # told, even though it's still correct in the structured data.
    assert "+15095550142" in t.answer, "the callback number rides along in the handoff line"
    assert t.raw["contact_hint"]["store"] == "yakima"
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 5 ─ and she still wants to buy something. The dispute turns leave no residue: this one
    # routes to inventory with the budget she just named.
    # REGRESSION (found 2026-08-07, left failing on purpose): _recent_escalation looks at the
    # last 6 raw history entries (user+assistant interleaved), which is really only her last 3
    # turns — turn 2's "refund" is still inside that window here, so this brand-new, unrelated
    # shopping request gets swallowed by the dispute path instead of ever reaching
    # suggest_products.
    t = c.say("while I'm in there, do you have another cartridge under $40?")
    assert t.intent == "product_suggestion"
    assert t.escalated is False and t.next_action == "show_products"
    assert t.tools == ["faq_lookup", "suggest_products"]
    args = t.args("suggest_products")
    assert args["category"] == "cartridge" and args["price_max"] == 40.0
    assert fake_bt.calls["search"][-1]["slots"]["price_max"] == 40.0, "the budget reached the client"
    assert t.pick_names == ["Avitas GSC 0.5g Cart", "Jetty Blue Dream 1g Cart"]
    for pick in t.picks:
        assert pick["price_otd"] > 0
        assert "cost" not in pick and "margin" not in pick

    assert len(c.turns) == 5
    assert c.transcript.count("user:") == 5


@pytest.mark.django_db
def test_same_policy_asked_calmly_then_angrily(convo):
    """Pullman caller: the identical policy, asked four ways — only the language moves the flag."""
    c = convo(store="pullman")

    # 1 ─ a calm policy question. Note "carts" would normally derive a category; the FAQ-first
    # guard keeps a returns question from being answered with an upsell.
    t = c.say("hey quick question — what's your return policy on vape carts?")
    assert t.intent == "return_policy"
    assert t.grounded and t.sources
    assert t.escalated is False and t.next_action == "answer"
    assert t.tools == ["faq_lookup"], "a returns question must not turn into a product search"
    assert RETURNS_ROW_ANSWER in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 2 ─ one word later ("defective") the same caller is a dispute, and the policy document
    # answer arrives wrapped in the apology + staff handoff.
    t = c.say("and what if one of them turns out to be defective?")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True and t.next_action == "escalate"
    assert t.grounded and t.sources[0]["kind"] == "policy"
    assert "may be exchanged with no time limit" in t.answer
    assert "Please share your details for the pullman team" in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 3 ─ the money question. FIXED 2026-08-07: "money back" is now a dispute trigger in its own
    # right (fix #1), so this correctly stays escalated instead of resetting to a calm FAQ lookup.
    t = c.say("so would I actually get money back, or just a swap?")
    assert t.intent == "conflict_resolution", "fixed 2026-08-07: 'money back' is now an escalation trigger"
    assert t.escalated is True and t.next_action == "escalate"
    # FIXED 2026-08-07 (was a REGRESSION left failing on purpose): "money back" is now in
    # _DISPUTE_TOPIC_RE too, so the relevance gate stays open and she's told plainly again.
    assert t.grounded and t.sources
    assert "cannot accept returns" in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 4 ─ she signs off. Escalation persists into this turn (turns 2-3 both tripped a dispute
    # trigger, and both are still inside the lookback window) — see the REAL REGRESSION note below
    # for what actually happens on the KB-row front.
    t = c.say("alright, I'll bring the box in")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True and t.next_action == "escalate"
    # REAL REGRESSION (confirmed 2026-08-07, left failing on purpose — do not weaken): "box" was
    # added to _DISPUTE_TOPIC_RE so the relevance gate stays open for on-topic packaging/receipt
    # follow-ups, but faq_lookup's retrieval has no topic awareness of its own — for THIS message
    # it ranks the unrelated "Do I need to bring ID?" FAQ row first (shared word "bring") and that
    # gets wrapped in the apology and spoken as if authoritative. That's exactly the failure mode
    # the relevance gate exists to prevent (see chat.py's comment about the loyalty-program row
    # read to an angry caller). Correct behaviour is still the generic, ungrounded handoff below.
    assert t.grounded is False and t.sources == []
    assert t.answer == (
        "I'm sorry that happened. I can't confirm a return or refund outcome from the current "
        "Happy Time knowledge base, but I can get the store team involved. "
        "Please share your details for the pullman team so they can contact you at a callback number or email."
    )

    assert len(c.turns) == 4


@pytest.mark.django_db
def test_money_back_demand_without_the_word_refund(convo):
    """Mount Vernon caller wants money back for a dead pen — the word she uses decides the route."""
    c = convo(store="mount-vernon", phone="3605550188")

    # 1 ─ FIXED 2026-08-07 (was FINDING 5, the sharp one): "money back" and "busted" are now
    # escalation triggers (fix #1), so this is correctly routed to a dispute instead of being
    # answered with three carts to buy.
    t = c.say("I want my money back for that busted vape pen")
    assert t.intent == "conflict_resolution", "fixed 2026-08-07: money-back demand now escalates"
    assert t.escalated is True and t.next_action == "escalate"
    assert t.tools == ["faq_lookup"], "an escalated turn must not shop at her"
    assert t.picks == []
    # FIXED 2026-08-07 (was a REGRESSION left failing on purpose): "money back" and "busted" are
    # now in _DISPUTE_TOPIC_RE, so the relevance gate stays open and a real grounded row is
    # spoken. It's the dedicated "Return policy" KB document (RETURN_POLICY_BODY, kind=policy)
    # rather than the FAQ_ROWS entry — same WAC citation, same no-refund-only-exchange remedy,
    # just a different (higher-weighted) source ranked first.
    assert t.grounded is True and RETURN_POLICY_BODY in t.answer
    assert t.answer.startswith("I'm sorry that happened.")

    # 2 ─ she says the word. Now it routes correctly: dispute, KB policy, staff handoff — and
    # crucially the upsell stops, even though "cart" is still in the sentence.
    t = c.say("no, I don't want another cart, I want a refund")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True and t.next_action == "escalate"
    assert t.tools == ["faq_lookup"], "an escalated turn must not shop at her"
    assert t.grounded and RETURNS_ROW_ANSWER in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)
    assert "+13605550188" in t.answer
    assert t.raw["contact_hint"] == {"store": "mount-vernon", "customer_phone": "+13605550188"}

    # 3 ─ she describes the defect. Same route, same grounded remedy, still no promise.
    t = c.say("the pen was dead out of the box, it doesn't work")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True and t.next_action == "escalate"
    assert t.tools == ["faq_lookup"]
    assert RETURNS_ROW_ANSWER in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    assert len(c.turns) == 3
