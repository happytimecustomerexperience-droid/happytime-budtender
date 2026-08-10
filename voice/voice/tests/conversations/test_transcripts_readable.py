"""Readable transcripts — hear what the agent ACTUALLY says, not just whether a flag is right.

The other thread files assert. This one narrates: it replays realistic calls end to end and
prints each one as a conversation, with the routing diagnostics under every reply. Run it when
you want to judge the agent's voice and judgement rather than its return values:

    uv run pytest voice/tests/conversations/test_transcripts_readable.py -s -q

It still asserts the floor that must never break on ANY turn — a non-empty reply, no cost/margin
leak, and a real intent label — so it fails if the agent goes mute or leaks, but its job is to be
read. Offline: seeded KB + FakeBudtender, exactly like the other threads.
"""

from __future__ import annotations

import json

import pytest

# (title, store, phone, [caller lines]) — each is one continuous call.
SCENARIOS = [
    ("Hours and directions", "yakima", "", [
        "hi, what time do you close today",
        "and where exactly are you located",
    ]),
    ("Specials hunter", "yakima", "", [
        "what specials do you have going on",
        "anything cheaper than that",
    ]),
    ("Flower by effect and budget", "yakima", "", [
        "I'm looking for some flower that helps me sleep",
        "keep it under 40 though",
    ]),
    ("Edible first-timer", "yakima", "", [
        "my friend wants to try edibles, what do you recommend",
        "how much is too much for a first time",
    ]),
    ("Cartridge shopper, plural phrasing", "yakima", "", [
        "do you have any carts for daytime focus",
        "what concentrates do you have too",
    ]),
    ("Pre-roll ask", "yakima", "", [
        "what's the cheapest pre-roll you have",
    ]),
    ("Defective cartridge return", "mount-vernon", "+15095551234", [
        "the cart I bought last night is broken, it won't fire",
        "do I need the receipt and the original packaging",
        "I'd like someone to call me back about it",
    ]),
    ("Money-back demand, no trigger word", "yakima", "", [
        "I want my money back for that busted vape pen",
    ]),
    ("Angry wrong item", "yakima", "+15095550147", [
        "someone put the wrong product in my bag and I want a refund",
        "seriously, this is unacceptable, I need a manager today",
    ]),
    ("Complains, then buys anyway", "yakima", "", [
        "this is frustrating, I want to talk to a person",
        "anyway, got any gummies while I'm here",
    ]),
    ("Numbers-guard probe", "yakima", "", [
        "exactly how many milligrams of THC is in the blue dream",
        "just give me your best guess",
    ]),
    ("Compliance and ID", "pullman", "", [
        "do I need to bring my ID",
        "what's your return policy on cannabis products",
    ]),
    # FIXED 2026-08-10 — the two gates newly added to answer_text_chat.
    ("Vendor callback", "yakima", "", [
        "hi, this is Dana with Cascade Crest Distribution, I'm calling about wholesale pricing on your cartridge line",
        "no rush, just have someone call me back about the manifest",
    ]),
    ("Order-ahead staging", "yakima", "+15095551234", [
        "I need a full gram cart under $40, something uplifting for daytime",
        "great, can you set that aside for me so it's ready when I get there",
    ]),
]


def _fmt(turn) -> str:
    bits = [f"intent={turn.intent}", "grounded" if turn.grounded else "UNGROUNDED",
            f"next={turn.next_action}"]
    if turn.escalated:
        bits.append("ESCALATE")
    if turn.tools:
        bits.append(f"tools={','.join(turn.tools)}")
    args = turn.args("suggest_products")
    if args:
        bits.append("slots=" + json.dumps({k: v for k, v in args.items() if k != "store"}))
    if turn.pick_names:
        bits.append(f"picks={turn.pick_names}")
    return "  [" + " | ".join(bits) + "]"


@pytest.mark.django_db
@pytest.mark.parametrize("title,store,phone,lines", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_transcript(convo, title, store, phone, lines):
    c = convo(store=store, phone=phone)
    print(f"\n{'=' * 78}\n{title}   (store={store}{', known caller' if phone else ''})\n{'=' * 78}")
    for line in lines:
        turn = c.say(line)
        print(f"\nCALLER: {line}")
        print(f"AGENT : {turn.answer}")
        print(_fmt(turn))

        # The floor every turn must clear, whatever the scenario.
        assert turn.answer.strip(), "the agent went silent"
        assert turn.intent, "no intent label"
        blob = json.dumps(turn.raw).lower()
        assert "margin" not in blob and '"cost"' not in blob, "leak-guard breach"
