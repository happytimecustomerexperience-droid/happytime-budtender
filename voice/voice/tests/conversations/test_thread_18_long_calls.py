"""Thread 18 — four long calls (15-25 turns each), the length real dispensary calls actually run.

Nothing else in this suite runs a call this long in one continuous ``convo``. That length is
exactly where state bugs live: a carried category going stale, escalation never clearing, history
truncation (``chat.py._history_text``/``_carried_category`` only look at the last 8 messages —
roughly the last 4 turns), and slots leaking between unrelated asks. Each call below prints every
turn like ``test_transcripts_readable.py`` so ``-s`` gives a readable transcript, and asserts the
floor every turn must clear plus scenario-specific behaviour. Where current behaviour is wrong for
a caller, it is pinned with a ``# GAP:`` comment instead of silently passing or being "fixed" here
(chat.py is out of scope for this file).
"""

from __future__ import annotations

import json
import re
import sys

import pytest

# Windows consoles default to cp1252, which chokes on the odd em-dash/approx-equal character a
# grounded KB row can legitimately contain. Widen stdout so ``-s`` transcripts never crash on
# encoding rather than on an actual assertion.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return set(_NUM_RE.findall(text or ""))


def _tool_numbers(picks: list[dict]) -> set[str]:
    out: set[str] = set()
    for pick in picks:
        for key in ("name", "brand", "strain", "why_this", "price_spoken", "price_otd", "thc_percent"):
            out |= _numbers(str(pick.get(key) or ""))
    return out


def _print_turn(n: int, caller: str, turn) -> None:
    print(f"\n[{n:02d}] CALLER: {caller}")
    print(f"     AGENT : {turn.answer}")
    args = turn.args("suggest_products")
    bits = [f"intent={turn.intent}", "grounded" if turn.grounded else "UNGROUNDED"]
    if turn.escalated:
        bits.append("ESCALATE")
    if turn.tools:
        bits.append(f"tools={','.join(turn.tools)}")
    if args:
        bits.append("slots=" + json.dumps({k: v for k, v in args.items() if k != "store"}))
    if turn.pick_names:
        bits.append(f"picks={turn.pick_names}")
    print("     [" + " | ".join(bits) + "]")


def _assert_floor(turn) -> None:
    """The floor every turn of every long call must clear, whatever the scenario."""
    assert turn.answer.strip(), "the agent went silent"
    assert turn.intent, "no intent label"
    blob = json.dumps(turn.raw).lower()
    assert "margin" not in blob and '"cost"' not in blob, "leak-guard breach"


def _assert_no_invented_number(turn) -> None:
    """Numbers-Guard: on an UNGROUNDED turn no digit may appear that wasn't already sitting in the
    caller's own supplied contact number — nothing composed in prose. A grounded turn (a real KB
    row, or live tool picks) legitimately carries real numbers; those are checked separately
    against their source, not banned outright."""
    if turn.grounded:
        return
    contact = turn.raw.get("contact_hint") or {}
    phone_digits = set(re.findall(r"\d+", str(contact.get("customer_phone") or "")))
    leftover = _numbers(turn.answer) - phone_digits
    assert not leftover, f"invented number on an ungrounded turn: {turn.answer!r} extra={leftover}"


# ════════════════════════════════════════════════════════════════════════════════
# Call 1 — the browser: wanders across categories, refines, detours, picks something.
# ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db
def test_the_browser_wanders_categories_and_never_carries_a_stale_one(convo, fake_bt):
    c = convo(store="yakima")
    print(f"\n{'=' * 78}\nCall 1 — The browser (store=yakima)\n{'=' * 78}")

    # 1) Opens on flower.
    t = c.say("hi, show me some flower")
    _print_turn(1, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert t.intent == "product_suggestion"
    assert t.args("suggest_products")["category"] == "flower"
    assert t.picks

    # 2) A refinement (price ceiling + effect) with no category word of its own — must carry
    #    flower forward from turn 1 via ``_carried_category``.
    t = c.say("something relaxing, keep it under $40 though")
    _print_turn(2, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "flower", "the refinement must carry the CURRENT (flower) category"
    assert args["price_max"] == 40.0
    assert args["effect_desired"] == "relaxed"
    assert all(p["sku"].startswith("FL-") for p in t.picks), t.pick_names

    # 3) Switches out loud to edibles — the new category must win outright, with nothing from the
    #    flower refinement (subcategory/price/effect) leaking across the switch.
    t = c.say("actually, do you have edibles instead")
    _print_turn(3, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "edible", "category tracks the CURRENT ask, not the flower turn"
    assert "price_max" not in args, "turn 2's $40 ceiling must not leak into the switch"
    assert "effect_desired" not in args, "turn 2's 'relaxed' must not leak into the switch"
    assert all(p["sku"].startswith("ED-") for p in t.picks), t.pick_names

    # 4) A refinement AFTER the switch — must refine the NEW (edible) category, not flower.
    t = c.say("something cheaper please")
    _print_turn(4, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "edible", "a refinement after a switch refines the NEW category"
    assert all(p["sku"].startswith("ED-") for p in t.picks), t.pick_names

    # 5) On to carts.
    t = c.say("hmm, what about carts then")
    _print_turn(5, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert t.args("suggest_products")["category"] == "cartridge"
    assert all(p["sku"].startswith("CT-") for p in t.picks), t.pick_names

    # 6) A refinement inside carts — must land on cartridge, not edible or flower.
    t = c.say("keep it under $30")
    _print_turn(6, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "cartridge", "the refinement carries the CURRENT (cart) category"
    assert args["price_max"] == 30.0
    assert all(p["sku"].startswith("CT-") for p in t.picks), t.pick_names

    # 7) On to pre-rolls.
    t = c.say("actually let me see pre-rolls, what's cheapest")
    _print_turn(7, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert t.args("suggest_products")["category"] == "pre-roll"
    assert all(p["sku"].startswith("PR-") for p in t.picks), t.pick_names

    # 8) FAQ detour: hours.
    t = c.say("quick question, what time do you close today")
    _print_turn(8, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert t.intent == "hours_location"
    assert t.tools == ["faq_lookup"]
    assert t.picks == []

    # 9) FAQ detour: specials.
    t = c.say("and are there any specials running right now")
    _print_turn(9, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert t.intent == "specials"
    assert t.tools == ["faq_lookup"]
    assert t.picks == []

    # 10) Back to flower, explicitly, with a fresh budget.
    t = c.say("okay, back to flower — anything under $35")
    _print_turn(10, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert args["price_max"] == 35.0
    assert all(p["sku"].startswith("FL-") for p in t.picks), t.pick_names

    # 11) A bare refinement right after — must resolve against flower (the RECENT category, from
    #     turn 10), not any of the earlier categories still sitting further back in history.
    t = c.say("something smaller")
    _print_turn(11, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "flower", "the bare refinement resolves against the RECENT category"
    assert all(p["sku"].startswith("FL-") for p in t.picks), t.pick_names

    # ── history-window probe ────────────────────────────────────────────────────
    # FIXED 2026-08-08: ``_carried_category`` now scans ``history[-20:]`` instead of
    # ``history[-8:]``, deliberately wider than ``_recent_escalation``'s 8-message dispute
    # window — shopping context should survive a tangent, a dispute should not. Push flower
    # (turn 10/11) through FAQ-only filler turns and confirm a bare refinement still reaches
    # back and resolves to flower instead of falling off the product path.
    filler = [
        ("hours_location", "what time do you open tomorrow"),
        ("specials", "any deals for new customers"),
        ("hours_location", "and where are you located exactly"),
        # "walk-ins"/"appointment" match none of ``_faq_topic``'s three buckets or
        # ``_FAQ_FIRST_RE``'s keyword list, so the intent LABEL falls to greeting_other even
        # though the answer itself is a genuine grounded KB hit (payment methods).
        ("greeting_other", "do you take walk-ins or is it appointment only"),
    ]
    for i, (expected_intent, line) in enumerate(filler, start=12):
        t = c.say(line)
        _print_turn(i, t.said, t)
        _assert_floor(t)
        _assert_no_invented_number(t)
        assert t.tools == ["faq_lookup"]
        assert t.picks == []
        assert t.intent == expected_intent, f"{line!r} -> {t.intent!r}, expected {expected_intent!r}"

    # Turns 10/11 (the only "flower" mentions so far) are still inside the wider history[-20:]
    # window (4 filler turns x 2 messages = 8 messages of FAQ chatter is well under 20), so a bare
    # refinement here now DOES carry flower forward — the lost-sale gap from the 8-message window
    # is closed.
    t = c.say("okay something a bit stronger then")
    _print_turn(16, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "flower", (
        "the wider 20-message window now carries flower across a 4-turn FAQ detour"
    )
    assert t.picks

    # ── stale-carry guard ───────────────────────────────────────────────────────
    # Widening the window makes stale-carry risk bigger, not smaller, so prove the other side too:
    # an explicit category SWITCH mid-detour must win, and a later bare refinement must refine the
    # NEW category — never fall back to the older, more-stale one still sitting in the window.
    t = c.say("actually never mind flower, what carts do you have")
    _print_turn(17, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "cartridge", "an explicit new subject overrides the carried category"
    assert t.picks

    t = c.say("something a bit stronger")
    _print_turn(18, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "cartridge", (
        "a bare refinement after an explicit switch refines the NEW category, not the stale flower one"
    )
    assert t.picks

    # 12) She restates the category explicitly — still lands cleanly, proving the router itself is
    #     fine on top of the carried-category fix.
    t = c.say("sorry, I meant flower — something stronger than the last one")
    _print_turn(19, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert t.picks

    # 13) Finally picks something and the call wraps.
    t = c.say("alright, I'll just grab the Gorilla Glue then, thanks")
    _print_turn(20, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)

    assert len(c.turns) == 20
    # Sanity: every product turn that DID reach the shelf tracked its own current category, never
    # a stale one — the whole point of the call.
    product_turns = [turn for turn in c.turns if "suggest_products" in turn.tools]
    assert len(product_turns) >= 10, "the browser's many category asks mostly reached the shelf"


# ════════════════════════════════════════════════════════════════════════════════
# Call 2 — the dispute that resolves: escalates, calms down, buys something else.
# ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db
def test_the_dispute_that_resolves_then_the_lost_sale_is_recovered(convo, fake_bt):
    c = convo(store="yakima")
    print(f"\n{'=' * 78}\nCall 2 — The dispute that resolves (store=yakima)\n{'=' * 78}")

    # 1) Opens angry about a broken cart.
    t = c.say("hi, the cart I bought is completely busted, it won't fire at all")
    _print_turn(1, t.said, t)
    _assert_floor(t)
    assert t.intent == "conflict_resolution"
    assert t.escalated
    assert t.next_action == "escalate"

    # 2) Escalates further.
    t = c.say("this is unacceptable, I want a refund right now")
    _print_turn(2, t.said, t)
    _assert_floor(t)
    assert t.intent == "conflict_resolution"
    assert t.escalated

    # 3) Gets return-policy info mid-dispute — grounded, escalation still live.
    t = c.say("what is your return policy anyway")
    _print_turn(3, t.said, t)
    _assert_floor(t)
    assert t.intent == "conflict_resolution"
    assert t.escalated and t.grounded
    assert "return" in " ".join(str(s.get("title", "")) for s in t.sources).lower()

    # 4) Still upset, no trigger word this turn — escalation must persist (``_recent_escalation``
    #    looks back over her last 6 messages).
    t = c.say("so what happens now, when do I hear back")
    _print_turn(4, t.said, t)
    _assert_floor(t)
    assert t.escalated, "escalation carries across a turn with no trigger word of its own"
    assert t.intent == "conflict_resolution"

    # 5) She reads her number out.
    c.phone = "509-555-0199"
    t = c.say("fine, my number is 509-555-0199, have someone call me")
    _print_turn(5, t.said, t)
    _assert_floor(t)
    assert t.escalated
    assert t.raw["contact_hint"] == {"store": "yakima", "customer_phone": "+15095550199"}

    # 6) She calms down and asks something unrelated.
    t = c.say("anyway while I've got you, what time do you close")
    _print_turn(6, t.said, t)
    _assert_floor(t)
    # A clean, non-escalation new question in the same turn ends the carried dispute: chat.py's
    # ``carried`` guard only keeps escalation alive if the turn does NOT name a fresh category —
    # "what time do you close" is an hours question, not a product ask, so the trigger word check
    # (``_HUMAN_RE``) is what actually decides this turn, and none of its words match.
    assert not t.escalated, "a clean hours question carries no dispute trigger word"
    assert t.intent == "hours_location"

    # 7) Filler FAQ turns to run the call long and push the dispute further back in history.
    t = c.say("and where are you located")
    _print_turn(7, t.said, t)
    _assert_floor(t)
    assert not t.escalated
    assert t.intent == "hours_location"

    t = c.say("any specials on right now")
    _print_turn(8, t.said, t)
    _assert_floor(t)
    assert not t.escalated
    assert t.intent == "specials"

    t = c.say("do you take walk-ins")
    _print_turn(9, t.said, t)
    _assert_floor(t)
    assert not t.escalated

    # 10) THE LOST-SALE ASSERTION: a clean product ask, well clear of the dispute now, must
    #     actually reach the shelf — not get swallowed by the earlier escalation.
    t = c.say("okay, got any gummies while I'm here")
    _print_turn(10, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert not t.escalated, "the dispute must not resurrect on a clean product ask"
    assert t.intent == "product_suggestion"
    assert t.args("suggest_products")["category"] == "edible"
    assert t.picks, "the lost-sale rule: the clean ask after a resolved dispute reaches the shelf"
    assert fake_bt.calls["search"][-1]["slots"]["category"] == "edible"

    # 11) Refines the purchase.
    t = c.say("something cheap, under $10")
    _print_turn(11, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert not t.escalated
    args = t.args("suggest_products")
    assert args["category"] == "edible"
    assert args["price_max"] == 10.0
    assert t.picks

    # 12) More FAQ, to prove escalation genuinely never resurrects on later unrelated turns.
    t = c.say("do I need to bring my ID")
    _print_turn(12, t.said, t)
    _assert_floor(t)
    assert not t.escalated
    assert t.intent != "conflict_resolution"

    t = c.say("what's your return policy on cannabis products")
    _print_turn(13, t.said, t)
    _assert_floor(t)
    assert not t.escalated, "GAP GUARD: a calm return-policy question must not resurrect the dispute"
    assert t.intent == "return_policy"

    # 13) One more product ask, further still from the original dispute.
    t = c.say("actually, add a cheap single pre-roll too, under $10")
    _print_turn(14, t.said, t)
    _assert_floor(t)
    _assert_no_invented_number(t)
    assert not t.escalated
    args = t.args("suggest_products")
    assert args["category"] == "pre-roll"
    assert args["price_max"] == 10.0
    assert t.picks

    # 14) A late, mild grumble that DOES carry a trigger word ("broken") — proves escalation can
    #     fire fresh again later in the call; resolving once does not permanently disable it.
    t = c.say("by the way I'm still a little annoyed about that broken cart honestly")
    _print_turn(15, t.said, t)
    _assert_floor(t)
    assert t.escalated, "'broken' re-trips _HUMAN_RE even framed mildly as 'annoyed'"
    assert t.intent == "conflict_resolution"

    # 15) The very next turn, still inside ``_recent_escalation``'s 6-message (3-turn) lookback,
    #     so the fresh "broken" complaint from turn 15 legitimately still carries here.
    t = c.say("never mind, that's everything for today, thanks")
    _print_turn(16, t.said, t)
    _assert_floor(t)
    assert t.escalated, "still inside the 3-turn lookback of turn 15's 'broken' trigger"

    assert len(c.turns) == 16
    escalated_flags = [turn.escalated for turn in c.turns]
    assert escalated_flags == [True, True, True, True, True, False, False, False, False,
                                False, False, False, False, False, True, True], escalated_flags
    # The core lost-sale proof: nine clean, unrelated turns (6-14) after the FIRST dispute
    # resolved, and escalation never resurrected on any of them without a fresh trigger word.
    assert escalated_flags[5:14] == [False] * 9, "no resurrection across nine unrelated turns"


# ════════════════════════════════════════════════════════════════════════════════
# Call 3 — the interrogator: 20 turns the KB cannot answer with a number.
# ════════════════════════════════════════════════════════════════════════════════

_INTERROGATOR_LINES = [
    "exactly how many milligrams of THC are in the Blue Dream cart",
    "what's the precise terpene percentage breakdown on that",
    "what harvest date was the Blueberry OG flower harvested on",
    "can you give me the lab batch number for the Gorilla Glue",
    "what's the exact CBD content down to the decimal",
    "how many milligrams of myrcene specifically",
    "what lab tested this batch and what was the test date",
    "give me the moisture content percentage",
    "what's the precise gram weight, not the label, the actual scale weight",
    "how many total cannabinoids in milligrams, exact number",
    "what's the pesticide test result in parts per million",
    "just give me your best guess on the THC percentage then",
    "okay, ballpark it for me, what do you THINK the terpene percentage is",
    "you must have SOME idea, give me a rough number",
    "what was the exact potency of the last batch you sold",
    "how many milligrams of CBG specifically",
    "what's the precise residual solvent level",
    "give me the exact microbial test count",
    "what date exactly was this cured",
    "one more time — your best guess, exact number, right now",
]


@pytest.mark.django_db
def test_the_interrogator_never_gets_an_invented_number(convo, fake_bt):
    c = convo(store="yakima")
    print(f"\n{'=' * 78}\nCall 3 — The interrogator (store=yakima)\n{'=' * 78}")

    for i, line in enumerate(_INTERROGATOR_LINES, start=1):
        t = c.say(line)
        _print_turn(i, line, t)
        _assert_floor(t)
        # Numbers-Guard core: on an ungrounded miss, no digit is invented. On a grounded turn
        # (either a real KB row, or live tool picks), any number spoken must trace back to that
        # tool result — it never "gets tired" partway through 20 turns and starts composing a
        # figure in prose instead.
        _assert_no_invented_number(t)
        if t.grounded and t.picks:
            assert _numbers(t.answer) <= _tool_numbers(t.picks), (
                f"turn {i} spoke a number the tool never returned: {t.answer!r}"
            )

    # A human is still offered as the questions keep failing to get real numbers — it must not
    # degrade into nonsense or stop offering the honest escape hatch.
    offered_human = sum(
        1 for turn in c.turns
        if turn.next_action in ("ask_staff", "escalate") or "my team" in turn.answer
    )
    assert offered_human >= 1, "the agent keeps offering a human rather than degrading into nonsense"
    assert len(c.turns) == len(_INTERROGATOR_LINES) == 20


# ════════════════════════════════════════════════════════════════════════════════
# Call 4 — the mixed long call: known caller, products + policy + staging + a vendor question.
# ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.django_db
def test_the_mixed_long_call_known_caller_intent_and_pii_floor_hold(convo, fake_bt):
    c = convo(store="mount-vernon", phone="+15095559876")
    print(f"\n{'=' * 78}\nCall 4 — The mixed long call, known caller (store=mount-vernon)\n{'=' * 78}")

    turns_meta = []  # (line, expected_intent_or_None)

    def _do(line, expected_intent=None, **kw):
        t = c.say(line)
        idx = len(c.turns)
        _print_turn(idx, line, t)
        _assert_floor(t)
        _assert_no_invented_number(t)
        # PII floor: leak-guard (cost/margin) holds on every turn, AND the only phone digits ever
        # spoken back to her are her OWN number, correctly normalized — never truncated, mangled,
        # or (this being a shared brain across callers) someone else's. Reading her own number
        # back as a callback contact (like thread 03 turn 4) is the intended behaviour, not a leak.
        blob = json.dumps(t.raw).lower()
        assert "cost" not in blob and '"margin"' not in blob
        spoken_digit_runs = re.findall(r"\d{10,11}", t.answer.replace("-", "").replace(" ", ""))
        for run in spoken_digit_runs:
            assert run.endswith("5095559876"), f"a phone-shaped number that isn't hers leaked: {run!r}"
        if expected_intent is not None:
            assert t.intent == expected_intent, f"{line!r} -> {t.intent!r}, expected {expected_intent!r}"
        turns_meta.append((line, t.intent))
        return t

    # 1) Opens shopping.
    t = _do("hi, I'm back — got any indica flower under $40", "product_suggestion")
    args = t.args("suggest_products")
    assert args["category"] == "flower"
    assert args["subcategory"] == "indica"
    assert args["price_max"] == 40.0
    assert fake_bt.calls["resume_by_phone"], "a known caller's phone resolves through recognition"

    # 2) A policy question. GAP: "do I need my ID on me" doesn't hit any of _faq_topic's three
    #    labelled buckets (return/specials/hours) or ``_FAQ_FIRST_RE``'s "id"/"identification"
    #    words in this exact phrasing, so it lands as an ungrounded ``general_faq`` miss instead
    #    of the ID-requirement answer the KB (per thread_09) actually has.
    t = _do("quick one — do I need my ID on me", "general_faq")
    assert not t.grounded

    # 3) Back to products, refinement.
    t = _do("okay and something cheaper than that")
    args = t.args("suggest_products")
    assert args["category"] == "flower", "the refinement tracks the CURRENT (flower) category"

    # 4) Phone-cart style staging ask (known gap: no stage_phone_cart branch in chat.py).
    t = _do("can you set that aside for me under my name so it's ready when I get there")
    assert "stage_phone_cart" not in t.tools
    # GAP: exactly like thread 07 — the shared text brain has no ``stage_phone_cart`` branch, so a
    # known caller's staging ask on a long call still gets answered with generic FAQ/greeting
    # copy instead of anything that actually reserves her picks.
    assert t.tools == ["faq_lookup"], "no phone-cart staging route exists from answer_text_chat"

    # 5) A vendor-sounding question dropped into an otherwise-retail call.
    t = _do("by the way, does anyone there handle wholesale purchasing for vendors")
    assert t.tools == ["faq_lookup"], "there is no vendor route from the text brain"
    # GAP: same as thread 06 — a vendor-sounding question mid-retail-call has no intent label of
    # its own and no notify_vendor_callback dispatch; it is read as an ordinary (ungrounded)
    # greeting/FAQ turn.
    assert "notify_vendor_callback" not in t.tools

    # 6) Back to a product ask — the router picks the thread back up cleanly after the detour.
    t = _do("anyway, what carts do you have around $30", "product_suggestion")
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"

    # 7) Another policy question.
    t = _do("what's your return policy")
    assert t.intent == "return_policy"
    assert t.grounded

    # 8) Refinement inside carts.
    t = _do("something a bit stronger")
    args = t.args("suggest_products")
    assert args["category"] == "cartridge", "refinement tracks the CURRENT (cart) category"

    # 9) Hours question.
    t = _do("what time do you close tonight", "hours_location")

    # 10) Specials question.
    t = _do("any specials I should know about", "specials")

    # 11) Another vendor-sounding aside, later in the call — must behave the same as turn 5.
    t = _do("also I've got a manifest to drop off if that matters")
    assert "notify_vendor_callback" not in t.tools

    # 12) Edibles ask.
    t = _do("switching gears, got any low dose edibles", "product_suggestion")
    args = t.args("suggest_products")
    assert args["category"] == "edible"

    # 13) Refinement.
    t = _do("cheaper please")
    args = t.args("suggest_products")
    assert args["category"] == "edible"

    # 14) Escalation mid-call — a genuine complaint interrupts the shopping.
    t = _do("actually hold on, the last thing I bought here was defective and won't fire")
    assert t.intent == "conflict_resolution"
    assert t.escalated

    # 15) Escalation carries without a trigger word.
    t = _do("so what's going to happen about that")
    assert t.escalated, "escalation carries across a turn with no trigger word of its own"

    # 16) She resolves it and buys again — the lost-sale rule, same as call 2.
    t = _do("never mind for now, just get me a couple pre-rolls, cheap ones", "product_suggestion")
    assert not t.escalated, "a clean product ask ends the carried dispute"
    args = t.args("suggest_products")
    assert args["category"] == "pre-roll"
    assert t.picks

    # 17) Concentrate ask.
    t = _do("what concentrates do you carry", "product_suggestion")
    args = t.args("suggest_products")
    assert args["category"] == "concentrate"

    # 18) Compliance/ID question.
    t = _do("do I need to bring ID every time")

    # 19) A last refinement — DOH-only filter. Says the category word again explicitly: a bare
    #     "medically compliant option" with no category of its own is NOT a recognised refinement
    #     (``_REFINEMENT_RE`` has no medical/compliance words), so it would otherwise fall through
    #     to the FAQ miss instead of narrowing the concentrate search — pinned via the sibling
    #     assertion right below.
    t = _do("actually is there a medically compliant concentrate option", "product_suggestion")
    args = t.args("suggest_products")
    assert args["category"] == "concentrate"
    assert args.get("doh_only") is True

    # FIXED 2026-08-08: ``_REFINEMENT_RE`` now also matches "medically compliant" and "doh", so a
    # bare "medically compliant" follow-up right after being shown concentrates carries that
    # category (and the still-live ``doh_only`` filter) forward instead of missing the shelf.
    bare = _do("what about just medically compliant, nothing else", "product_suggestion")
    args = bare.args("suggest_products")
    assert args["category"] == "concentrate", (
        "'medically compliant' alone now carries the concentrate category forward"
    )
    assert args.get("doh_only") is True, "the DOH-only filter from the prior turn rides along too"
    assert bare.picks

    # 21) Wraps the call.
    t = _do("that's everything, thank you")

    assert len(c.turns) == 21
    # Intent tracked correctly turn by turn: every turn that named a fresh category (or, since the
    # refinement fix, carried one forward) landed on product_suggestion, and none of the
    # vendor/staging detours were mistaken for one.
    product_intents = {i for i, (_, intent) in enumerate(turns_meta, start=1) if intent == "product_suggestion"}
    assert product_intents == {1, 3, 6, 8, 12, 13, 16, 17, 19, 20}, product_intents

    # Leak-guard + PII floor held on EVERY single turn of the whole call.
    for turn in c.turns:
        blob = json.dumps(turn.raw).lower()
        assert "cost" not in blob and '"margin"' not in blob
        for run in re.findall(r"\d{10,11}", turn.answer.replace("-", "").replace(" ", "")):
            assert run.endswith("5095559876")
