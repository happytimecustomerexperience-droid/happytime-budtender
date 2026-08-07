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

    # 3 ─ the natural follow-up. It is unmistakably about the return she just asked about, but the
    # router reads THIS message only.
    t = c.say("do I need the original packaging and the receipt?")
    assert t.grounded and t.sources
    assert t.sources[0]["kind"] == "policy"
    assert "original packaging" in t.answer and "receipt" in t.answer
    # FINDING 1 — the complaint context is dropped: no history is consulted for the label or the
    # escalation flag, so a mid-dispute turn is filed as small talk and the handoff flag resets.
    assert t.intent == "greeting_other", "expected return_policy/conflict_resolution given turns 1-2"
    assert t.escalated is False and t.next_action == "answer"
    # FINDING 2 — the policy row is read out whole, including the sentence that is addressed to the
    # AGENT rather than the customer. Dana hears the agent's own operating instructions.
    assert "the agent never promises a refund or adjudicates a dispute itself" in t.answer
    assert RETURN_POLICY_BODY in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 4 ─ she asks for a person. The handoff itself is right: flagged, routed, contact captured.
    t = c.say("I'd really like a staff member to call me back about it")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True and t.next_action == "escalate"
    assert "+15095550142" in t.answer, "the callback number rides along in the handoff line"
    assert t.raw["contact_hint"]["store"] == "yakima"
    assert t.tools == ["faq_lookup"]
    # FINDING 3 — grounded, but on the wrong row: the retriever's best keyword hit is the EVENTS
    # FAQ, and the escalation branch speaks whatever came back. The apology is followed by a
    # promo blurb, and the return policy she is mid-dispute about is never repeated.
    assert t.grounded is True
    assert "events" in t.sources[0]["title"].lower()
    assert "exchanged" not in t.answer.lower()
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 5 ─ and she still wants to buy something. The dispute turns leave no residue: this one
    # routes to inventory with the budget she just named.
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

    # 3 ─ the money question, asked without any trigger word. Back to a plain answer — and the
    # answer holds the line: a resolution in store, never cash back.
    t = c.say("so would I actually get money back, or just a swap?")
    assert t.intent == "return_policy"
    assert t.escalated is False and t.next_action == "answer"
    assert t.grounded and t.sources
    assert "cannot accept returns" in t.answer
    assert not _promises_refund(t.answer), _promises_refund(t.answer)

    # 4 ─ she signs off. FINDING 4 — small talk still runs retrieval and still answers "grounded":
    # "bring the box in" keys on "bring" and she is told to bring photo ID.
    t = c.say("alright, I'll bring the box in")
    assert t.intent == "greeting_other"
    assert t.grounded is True and t.next_action == "answer"
    assert t.sources[0]["title"] == "Do I need to bring ID?"
    assert "photo ID" in t.answer

    assert len(c.turns) == 4


@pytest.mark.django_db
def test_money_back_demand_without_the_word_refund(convo):
    """Mount Vernon caller wants money back for a dead pen — the word she uses decides the route."""
    c = convo(store="mount-vernon", phone="3605550188")

    # 1 ─ FINDING 5 (the sharp one): "money back" and "busted" are not escalation triggers, but
    # "vape pen" IS a category trigger — so a refund demand is routed to the shelf and answered
    # with three carts to buy, flagged grounded, with no apology and no handoff.
    t = c.say("I want my money back for that busted vape pen")
    assert t.intent == "product_suggestion", "expected conflict_resolution on a money-back demand"
    assert t.escalated is False and t.next_action == "show_products"
    assert t.args("suggest_products")["category"] == "cartridge"
    assert len(t.picks) == 3
    assert t.grounded is True and t.sources == [{"kind": "tool", "title": "Live budtender inventory"}]
    assert "sorry" not in t.answer.lower()

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
