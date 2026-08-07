"""Thread 03 — Marisol got home with the wrong item in her bag: the conflict_resolution route end to end.

Proves escalation outranks a grounded FAQ row, that the reply apologises and asks for a callback
method, that safe_next_action stays ``escalate``, and that ``contact_hint`` carries store + phone
once she reads her number out. Turns marked FINDING assert the behaviour the router ACTUALLY has,
not the behaviour the scenario wanted — see the module's findings notes in each comment.
"""

from __future__ import annotations

import pytest

# The grounded return-policy row every escalation turn is measured against.
WAC = "WAC 314-55-079"


@pytest.mark.django_db
def test_angry_wrong_item_thread(convo, fake_bt):
    """One continuous call: complaint → refund demand → manager → callback number → follow-ups."""
    c = convo(store="yakima")

    # 1. The opening complaint. "wrong item" + "not what I ordered" are escalation triggers.
    t = c.say("hi, I just got home and the wrong item is in my bag, this is not what I ordered")
    assert t.intent == "conflict_resolution"
    assert t.escalated, "a wrong-item complaint must raise the escalation flag on turn one"
    assert t.raw["escalation_flag"] is True, "both escalation keys travel together"
    assert t.next_action == "escalate"
    assert t.tools == ["faq_lookup"], "a complaint never turns into a product pitch"
    assert t.answer.startswith("I'm sorry that happened."), "the reply has to apologise first"
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at "
        "a callback number or email."
    ), "and close by asking for a callback method"
    # FINDING 1: the escalation branch speaks whatever KB row ranked first, with no relevance
    # gate — the return-policy row exists (turn 2 reads it out) but this complaint does not
    # reach it. The apology and the contact ask bracket an unrelated grounded answer.
    assert t.grounded and t.sources, "chat.py still speaks a KB row on an escalation turn"
    assert WAC not in t.answer, (
        "FINDING: the opening wrong-item complaint is answered with an off-topic KB row, "
        "not the return policy"
    )

    # 2. She escalates it herself — and this time the FAQ genuinely matches. Escalation must win.
    t = c.say("I'm honestly pretty upset about it, can I just get a refund? what is your return policy")
    assert t.grounded and t.sources, "the return-policy row is a real grounded hit"
    assert any("return" in str(s.get("title", "")).lower() for s in t.sources)
    assert WAC in t.answer, "the grounded policy text is spoken verbatim from the KB"
    assert t.intent == "conflict_resolution", (
        "escalation outranks the grounded FAQ topic — a dispute is not a return_policy lookup"
    )
    assert t.escalated and t.next_action == "escalate", "grounded must not cancel the escalation"
    assert t.answer.startswith("I'm sorry that happened.")
    assert "a callback number or email" in t.answer
    # The structured hint is the only place the LOCATION ask survives (see FINDING 2 on turn 3).
    assert t.raw["safe_suggested_next_action"] == (
        "Please share the location and the best way for staff to follow up."
    )

    # 3. She refuses the exchange and asks for a manager. Still no phone on the session.
    t = c.say("no, I don't want an exchange, I want to speak to a manager")
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.next_action == "escalate"
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": ""}, (
        "before she gives a number the hint carries the store and an empty phone"
    )
    # FINDING 2: on the grounded escalation path the spoken reply asks ONLY for a contact method.
    # The "share the location and the best way for staff to follow up" wording lives in the
    # un-grounded escalation branch, which the KB's keyword fallback almost never reaches.
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at "
        "a callback number or email."
    ), "FINDING: the spoken escalation reply never asks the caller for their location"

    # 4. She reads her number out. From here the session carries it, like the widget would.
    c.phone = "509-555-0147"
    t = c.say("fine. my number is 509-555-0147, have the manager call me back")
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.next_action == "escalate"
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": "+15095550147"}, (
        "contact_hint must carry store + the normalized phone once she supplies it"
    )
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at +15095550147."
    ), "the spoken reply reads back the number staff will call"
    assert fake_bt.calls["resume_by_phone"][-1] == {"phone": "+15095550147", "location": "yakima"}, (
        "the raw number is normalized to E.164 and the store rides along to the profile lookup"
    )

    # 5. Mid-dispute, phrased without a trigger word.
    # FINDING 3: escalation is per-message keyword matching with no memory of the conversation,
    # so this drops straight off the escalation path — greeting_other, safe_next_action "answer",
    # no apology — even though the previous four turns were one unresolved dispute.
    t = c.say("so what are you going to do about it")
    assert not t.escalated, "FINDING: the escalation flag does not persist across turns"
    assert t.intent == "greeting_other", "FINDING: a mid-dispute turn is labelled greeting_other"
    assert t.next_action == "answer", "FINDING: safe_next_action falls back to answer mid-dispute"
    assert not t.answer.startswith("I'm sorry")
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": "+15095550147"}, (
        "the contact hint survives the drop even though the escalate action does not"
    )

    # 6. She says a trigger word again and the route comes straight back — purely keyword-driven.
    t = c.say("seriously, this is unacceptable, I need a manager to call me today")
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.next_action == "escalate"
    assert t.answer.startswith("I'm sorry that happened.")
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at +15095550147."
    )

    assert len(c.turns) == 6
    assert [turn.escalated for turn in c.turns] == [True, True, True, True, False, True]
    assert sum(1 for turn in c.turns if turn.next_action == "escalate") == 5


@pytest.mark.django_db
def test_same_policy_row_calm_then_angry(convo):
    """The KB row is identical either way — the escalation trigger is what flips the route."""
    c = convo(store="yakima")

    t = c.say("what is your return policy")
    assert t.intent == "return_policy", "a calm policy question is a plain FAQ lookup"
    assert t.grounded and WAC in t.answer
    assert not t.escalated
    assert t.next_action == "answer"
    assert not t.answer.startswith("I'm sorry that happened."), "nothing to apologise for yet"
    assert "Please share your details" not in t.answer, "and no contact ask on a calm answer"
    calm_policy_answer = t.answer

    t = c.say("and what if the cart is defective and won't fire")
    assert t.intent == "conflict_resolution", "'defective' / \"won't fire\" flip the same row to a dispute"
    assert t.escalated and t.next_action == "escalate"
    assert t.tools == ["faq_lookup"], "a defect complaint never becomes a cartridge suggestion"
    assert calm_policy_answer in t.answer, "the same grounded row, now wrapped in the escalation reply"
    assert t.answer.startswith("I'm sorry that happened.")
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at "
        "a callback number or email."
    )


@pytest.mark.django_db
def test_escalation_without_a_store_then_she_names_one(convo):
    """A web chat that starts with no store still escalates — but carries no contact_hint at all."""
    c = convo(store="")

    t = c.say("someone put the wrong product in my bag and I want a refund")
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.next_action == "escalate"
    assert t.grounded and WAC in t.answer
    # FINDING 4: with neither store nor phone the structured hint is dropped entirely, so an
    # escalation reaches staff with the ask in prose only and nothing machine-readable.
    assert t.raw["contact_hint"] is None, "FINDING: no store + no phone means no contact_hint"
    assert t.answer.endswith(
        "Please share your preferred contact method (a callback number or email) "
        "so the team can follow up."
    )
    assert t.raw["store"] == ""

    c.store = "mount-vernon"
    t = c.say("I was at the mount vernon store and I'm still mad about it")
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.next_action == "escalate"
    assert t.raw["contact_hint"] == {"store": "mount-vernon", "customer_phone": ""}, (
        "naming the store fills the hint even before a phone number exists"
    )
    assert t.answer.endswith(
        "Please share your details for the mount-vernon team so they can contact you at "
        "a callback number or email."
    )
