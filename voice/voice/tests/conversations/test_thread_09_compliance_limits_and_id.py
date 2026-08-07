"""Thread 09 - Dana, a first-time Pullman caller with a medical card: ID rules, the daily legal limit, and a DOH-compliant ask, proving policy turns must cite a KB source and the DOH slot reaches inventory."""

from __future__ import annotations

import pytest

from voice.tools import TOOL_REGISTRY


def _titles(turn) -> list[str]:
    return [str(s.get("title", "")).lower() for s in turn.sources]


@pytest.mark.django_db
def test_compliance_limits_and_doh_thread(convo, fake_bt):
    """One continuous call: ID rules -> the legal limit -> medical card -> a DOH product."""
    c = convo(store="pullman")

    # 1. The compliance route: an ID question is source-required, so it must be grounded AND cited.
    t = c.say("hi, it's my first time coming in - what ID do I need to bring?")
    assert t.intent == "general_faq"
    assert t.grounded, "an ID rule must come from the KB, never from the model"
    assert t.sources, "_requires_sources: a policy answer with no citation is not speakable"
    assert t.tools == ["faq_lookup"], "a policy question must not touch inventory"
    assert "government-issued photo id" in t.answer.lower()
    assert "21 or older" in t.answer
    assert t.next_action == "answer"

    # 2. Follow-up that only makes sense after turn 1 - the router keeps the ID topic.
    t = c.say("my partner only has an expired ID, is that okay?")
    assert t.intent == "general_faq"
    assert t.grounded and t.sources
    assert "unexpired" in t.answer.lower(), "the accepted-ID row, not the generic bring-ID row"
    assert "party" in t.answer.lower(), "everyone in the party needs ID"
    assert any("id" in title for title in _titles(t))

    # 3. The daily legal limit - a WA-law number that must be quoted from the KB verbatim.
    t = c.say("got it. what's the legal limit I can buy in one day?")
    assert t.intent == "general_faq"
    assert t.grounded and t.sources
    assert "1 ounce" in t.answer and "7 grams" in t.answer, "the WAC per-visit caps, quoted not invented"
    assert any("purchase limits" in title for title in _titles(t))
    assert t.tools == ["faq_lookup"]

    # 4. "does that limit change with a medical card" - FINDING (see report): 'medical'/'card'
    #    are in neither _SOURCE_REQUIRED_RE nor _FAQ_FIRST_RE, so this compliance question is
    #    labelled greeting_other, skips the citation guard, and the keyword ranker hands back an
    #    unrelated row. It is still grounded+cited (Numbers-Guard holds), just off-topic.
    t = c.say("does that limit change if I have a medical card?")
    assert t.intent == "greeting_other", "FINDING: a medical-limits question is not a policy intent"
    assert t.grounded and t.sources, "whatever it says still has to come from a real KB row"
    assert t.next_action == "answer"
    assert "ounce" not in t.answer.lower(), "FINDING: the reply never restates the limit it was asked about"
    assert "doh" not in t.answer.lower(), "FINDING: the DOH row exists in the KB but is not retrieved here"

    # 5. A bare DOH ask with no category word - FINDING: no inventory search happens at all,
    #    because chat.py only builds suggest args when a category is derivable from the text.
    t = c.say("ok, so do you carry anything DOH compliant?")
    assert t.tools == ["faq_lookup"], "FINDING: doh_only never reaches suggest_products without a category"
    assert t.intent == "greeting_other"
    assert t.grounded and t.sources
    assert "doh-compliant" in t.answer.lower(), "the WA age/ID store fact does mention the DOH filter"

    # 6. Same intent, now with a category - the DOH slot finally reaches the router and the client.
    t = c.say("I mean a DOH compliant concentrate")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["doh_only"] is True, "_DOH_ONLY_RE must derive doh_only from 'DOH compliant'"
    assert args["category"] == "concentrate"
    assert args["store"] == "pullman"
    assert t.pick_names == ["DOH Compliant RSO 1g"], "the only DOH row in the catalog"
    assert t.grounded and t.next_action == "show_products"
    for pick in t.picks:
        assert pick["price_otd"] > 0
        assert "cost" not in pick and "margin" not in pick

    # The derived slot survived the schema wall and reached the budtender client as a hard filter.
    searches = fake_bt.calls["search"]
    assert len(searches) == 1, "the five policy turns must never have hit inventory"
    assert searches[0]["slots"] == {"store": "pullman", "category": "concentrate", "doh_only": True}
    assert searches[0]["location"] == "pullman"

    # 7. Back to compliance after the product turn - still grounded, still cited.
    t = c.say("perfect - and I'll still need my ID at pickup for that, right?")
    assert t.intent == "general_faq"
    assert t.grounded and t.sources
    assert "photo id" in t.answer.lower() and "pickup" in t.answer.lower()
    assert t.tools == ["faq_lookup"]

    assert len(c.turns) == 7
    assert not any(turn.escalated for turn in c.turns), "a compliance call is not a dispute"
    # Every turn that DID get the source-required treatment carries its citation.
    for turn in (c.turns[0], c.turns[1], c.turns[2], c.turns[6]):
        assert turn.grounded and turn.sources


@pytest.mark.django_db
def test_uncited_policy_claim_is_never_spoken(convo, fake_bt, monkeypatch):
    """Fault injection: a KB tool that claims 'grounded' with no citation must be downgraded.

    The real KB always ships sources with a grounded row, so the ``_requires_sources`` guard in
    chat.py can only be exercised by making the tool misbehave. Here it returns a confident,
    uncited (and wrong) purchase-limit claim - the agent must refuse to speak it.
    """
    fabricated = "You can buy up to five ounces of flower a day at any Happy Time."

    def _uncited_faq(args, ctx):
        return {"answer": fabricated, "grounded": True, "sources": [], "store": ctx.get("store", "")}

    monkeypatch.setitem(TOOL_REGISTRY, "faq_lookup", _uncited_faq)
    c = convo(store="pullman")

    # No category in the text -> the plain policy path: downgraded to ungrounded, handed to staff.
    t = c.say("what is the legal daily purchase limit here?")
    assert fabricated not in t.answer, "an uncited policy claim must never be spoken as fact"
    assert t.grounded is False
    assert t.sources == []
    assert t.result("faq_lookup")["grounded"] is False, "the downgrade is recorded on the tool result"
    assert t.next_action == "ask_staff"
    assert "can't confirm" in t.answer.lower()
    assert t.intent == "general_faq"

    # Same uncited tool, but now the caller names a category, so the fallback is an inventory
    # pitch. The picks are real, yet the turn stays UNGROUNDED because the question was a policy
    # question (chat.py's policy_context) - products must not launder a compliance answer.
    t = c.say("ok, then is there a legal cap on how much flower I can get?")
    assert t.intent == "product_suggestion"
    assert t.picks, "the fake catalog has flower - an empty result would hide the real assertion"
    assert t.grounded is False, "a policy question answered with products is not a grounded answer"
    assert fabricated not in t.answer
    assert t.next_action == "show_products"
    assert fake_bt.calls["search"][-1]["slots"]["category"] == "flower"

    assert len(c.turns) == 2
