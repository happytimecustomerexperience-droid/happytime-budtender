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
    # FIXED 2026-08-07 (was FINDING 1): the relevance gate means a KB row is only spoken on an
    # escalation turn if the message itself asks about something the KB covers. "wrong item...
    # not what I ordered" doesn't match any FAQ trigger word, so the off-topic row that used to
    # get read out here (an apology wrapped around whatever ranked first) is gone — she gets the
    # honest, generic fallback instead. That fallback is now personalized via
    # ``_staff_followup_hint`` (2026-08-07), so it names the store even without a phone yet.
    assert t.grounded is False and t.sources == [], "chat.py no longer speaks an unrelated KB row"
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at a callback number or email."
    ), "the un-grounded escalation branch's personalized closing line"
    assert WAC not in t.answer, "the opening wrong-item complaint still isn't the return policy"

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
    # FIXED 2026-08-07 (was FINDING 2): "exchange" / "manager" don't match _FAQ_FIRST_RE either,
    # so this now correctly lands on the same generic, un-grounded handoff line as turn 1 —
    # consistent, personalized (still no phone yet), and no longer wrapped around an unrelated
    # grounded row.
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at a callback number or email."
    )

    # 4. She reads her number out. From here the session carries it, like the widget would.
    c.phone = "509-555-0147"
    t = c.say("fine. my number is 509-555-0147, have the manager call me back")
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.next_action == "escalate"
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": "+15095550147"}, (
        "contact_hint must carry store + the normalized phone once she supplies it"
    )
    # FIXED 2026-08-07 (was a REGRESSION left failing on purpose): the un-grounded escalation
    # branch now builds its reply through ``_escalation_answer`` -> ``_staff_followup_hint``, so
    # even though "manager"/"call me back" don't hit _FAQ_FIRST_RE, the spoken reply still reads
    # her number back — it no longer depends on retrieval happening to return some row.
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at +15095550147."
    ), "the spoken reply reads back the number staff will call"
    assert fake_bt.calls["resume_by_phone"][-1] == {"phone": "+15095550147", "location": "yakima"}, (
        "the raw number is normalized to E.164 and the store rides along to the profile lookup"
    )

    # 5. Mid-dispute, phrased without a trigger word.
    # FIXED 2026-08-07 (was FINDING 3, the escalation-memory bug): _recent_escalation looks back
    # over her last few turns, so a follow-up with no trigger word of its own ("so what are you
    # going to do about it") correctly stays inside the dispute instead of falling back to
    # greeting_other.
    t = c.say("so what are you going to do about it")
    assert t.escalated, "fixed 2026-08-07: escalation now persists across turns"
    assert t.intent == "conflict_resolution"
    assert t.next_action == "escalate"
    assert t.answer.startswith("I'm sorry that happened.")
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": "+15095550147"}, (
        "the contact hint carries through unchanged"
    )

    # 6. She says a trigger word again — still escalated (it never should have left).
    t = c.say("seriously, this is unacceptable, I need a manager to call me today")
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.next_action == "escalate"
    assert t.answer.startswith("I'm sorry that happened.")
    # Same as turn 4: the un-grounded branch is personalized now, so the known phone number is
    # still read back even though "unacceptable"/"manager" don't hit _FAQ_FIRST_RE.
    assert t.answer.endswith(
        "Please share your details for the yakima team so they can contact you at +15095550147."
    )

    assert len(c.turns) == 6
    assert [turn.escalated for turn in c.turns] == [True, True, True, True, True, True]
    assert sum(1 for turn in c.turns if turn.next_action == "escalate") == 6


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
    # FIXED 2026-08-07 (was a REGRESSION left failing on purpose): contact_hint carries the store
    # she just named, and the un-grounded branch's reply is personalized too now, so the spoken
    # line names the mount-vernon team even though "mount vernon store... still mad" doesn't hit
    # _FAQ_FIRST_RE. Same fix as thread 03 turns 4/6.
    assert t.answer.endswith(
        "Please share your details for the mount-vernon team so they can contact you at "
        "a callback number or email."
    )
