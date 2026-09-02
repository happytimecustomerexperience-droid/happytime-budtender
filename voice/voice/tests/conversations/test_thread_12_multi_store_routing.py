"""Thread 12 — Dana calls about Mount Vernon, then about Pullman: which store's facts the brain speaks.

The store has to ride into ``faq_lookup``'s args, come back on the result, pick the caller's own
address/phone rows, price inventory at that store's tax rate, treat "mt vernon" as Mount Vernon, and
— when the caller names a store we don't have — be dropped by ``chat._safe_store`` instead of passed
through. Where the brain does NOT do that today the assertion pins the REAL behaviour and says so in
a ``GAP`` comment; those are reported findings, not softened tests.
"""

from __future__ import annotations

import pytest

# The confirmed StoreFact figures (kb/seed.py STORE_FACT_ROWS) — the only numbers the agent may speak.
YAKIMA_ADDRESS, YAKIMA_PHONE = "1315 N 1st St", "(509) 571-1106"
MV_ADDRESS, MV_PHONE = "200 Suzanne Ln", "(360) 488-2923"
PULLMAN_ADDRESS, PULLMAN_PHONE = "5602 WA-270", "(509) 334-2788"

# The six questions Dana asks at each store, in order — the same call, twice.
SCRIPT = (
    "hi there, what are your hours",
    "and where are you located",
    "perfect. what is the address",
    "and what is your phone",
    "one more thing, any specials on edibles",
    "ok, do you have any gummies",
)


def _titles(turn) -> list[str]:
    return [str(s.get("title") or "") for s in turn.sources]


@pytest.mark.django_db
def test_mount_vernon_call_then_the_pullman_call(convo, fake_bt):
    """Dana runs the same six questions at Mount Vernon, then at Pullman."""
    mv = convo(store="mount-vernon")

    # 1 ── hours. The store reaches the tool and comes back on the result.
    t1 = mv.say(SCRIPT[0])
    assert t1.intent == "hours_location"
    assert t1.tools == ["faq_lookup"]
    assert t1.grounded, "hours must come from the KB, never invented"
    assert t1.args("faq_lookup")["store"] == "mount-vernon", "the store must ride into the tool args"
    assert t1.result("faq_lookup")["store"] == "mount-vernon", "and back out on the result"
    assert t1.raw["store"] == "mount-vernon"
    # Retrieval IS store-scoped: the only per-store hours row in play is Mount Vernon's.
    assert "Mt Vernon hours" in _titles(t1)
    assert "Yakima hours" not in _titles(t1) and "Pullman hours" not in _titles(t1)
    # FIXED (retrieval-precision follow-up): the row the agent actually SPOKE used to be the global
    # three-locations FAQ (Yakima's clock time, no Mount Vernon closing time at all) even though
    # Mount Vernon's own hours row was the one cited. chat.py now passes topic="hours_location",
    # which excludes that global FAQ from the corpus entirely, so the cited row is the spoken one.
    assert "9 AM" in t1.answer and "10 PM" in t1.answer, "Mt Vernon's own hours row is now spoken"
    assert "8AM to 11:30PM" not in t1.answer, "no longer reads Yakima's hours to a Mt Vernon caller"

    # 2 ── directions. FIXED (retrieval-precision follow-up): the winning row used to be a global
    # site FAQ with Yakima hard-coded in its body — Dana was confidently sent to the Yakima
    # address. topic="hours_location" now excludes that global FAQ, and the correct Mt Vernon
    # address row has no lexical bridge to "and where are you located" for the keyword fallback to
    # find, so retrieval safely declines instead of confidently sending her to the wrong address.
    t2 = mv.say(SCRIPT[1])
    assert t2.intent == "hours_location"
    assert t2.grounded is False
    assert YAKIMA_ADDRESS not in t2.answer, "no longer sent to Yakima's address"

    # 3 ── the address, asked plainly. Store-scoped, and correct.
    t3 = mv.say(SCRIPT[2])
    assert t3.grounded and t3.intent == "hours_location"
    assert MV_ADDRESS in t3.answer
    assert YAKIMA_ADDRESS not in t3.answer and PULLMAN_ADDRESS not in t3.answer
    assert _titles(t3)[0] == "Mt Vernon address", "the store's own row must rank first"
    # GAP: the answer is the raw KB chunk, so it opens with the internal slug the SPEAKING_RULES
    # forbid ("never an internal store code").
    assert t3.answer.startswith("mount-vernon Mt Vernon address:")

    # 4 ── the phone number, still on the Mount Vernon rows.
    t4 = mv.say(SCRIPT[3])
    assert MV_PHONE in t4.answer
    assert YAKIMA_PHONE not in t4.answer and PULLMAN_PHONE not in t4.answer
    assert _titles(t4)[0] == "Mt Vernon phone"

    # 5 ── specials. GAP: store-blind. The KB holds per-store special rows (Pullman runs 30% on
    # edibles, Mount Vernon 20%) but the global FAQ row still wins the top spot, so Dana hears every
    # store's deal and is then asked which store she's at — five turns after we already knew.
    # CHANGED (retrieval-precision follow-up): topic="specials" now narrows the corpus to
    # specials-only rows, so the per-store rows compete directly and DO surface among the sources
    # — just not as the top (spoken) one, so the GAP itself is unchanged.
    t5 = mv.say(SCRIPT[4])
    # UPDATED 2026-09-01: deals carry a validity window now (StoreFact.valid_from/valid_to)
    # and the only seeded set is July's, whose window has closed, so the specials answer is
    # the honest "nothing posted right now" — ungrounded by design (no KB row asserts an
    # absence). Post a current deal in the dashboard and this grounds on it again.
    assert t5.intent == "specials"
    assert t5.grounded or "specials posted" in t5.answer
    assert "Pullman" not in t5.answer, "a Mount Vernon caller never hears Pullman's deals"
    # The cross-store deal recital ("20% off at Yakima and Mount Vernon") came from the FAQ
    # row's hardcoded July prose, which is gone: a specials answer is now built from THIS
    # store's current rows, so it can only ever name the deals of the store being asked about.
    assert "Yakima" not in t5.answer
    # ...which is the whole GAP this line used to pin: a Mt Vernon caller was read Pullman's
    # deal. It cannot happen any more.
    # Nor is she asked which store she is at five turns after telling us: the answer is scoped
    # to her store already, so the question never has to be re-asked.
    assert "which store" not in t5.answer
    # The July rows are out of their window, so they are not sources either — an expired deal
    # is not evidence for anything. When a deal IS running its row is the source (pinned by
    # tests/test_kb.py::test_expired_special_is_never_spoken_and_a_current_one_is).
    assert not any(title.startswith("July:") for title in _titles(t5))

    # 6 ── a product ask: the store must reach the inventory search AND the OTD tax rate.
    t6 = mv.say(SCRIPT[5])
    assert t6.intent == "product_suggestion"
    assert t6.tools == ["faq_lookup", "suggest_products"]
    assert t6.args("suggest_products")["store"] == "mount-vernon"
    assert fake_bt.calls["search"][-1]["location"] == "mount-vernon"
    assert fake_bt.calls["search"][-1]["slots"]["store"] == "mount-vernon"
    assert t6.pick_names == ["Cannaquench Sparkling 5mg", "Wyld Raspberry Gummies 10mg"]
    mv_wyld = next(p for p in t6.picks if p["sku"] == "ED-WYLD-10")
    assert mv_wyld["price_otd"] == 15.0, "menu price, unchanged — tax-inclusive Dutchie account"

    assert len(mv.turns) == 6
    assert mv.transcript.count("user:") == 6

    # ── the same six questions, Pullman ──────────────────────────────────────────
    pu = convo(store="pullman")
    p1 = pu.say(SCRIPT[0])
    assert p1.args("faq_lookup")["store"] == "pullman"
    assert "Pullman hours" in _titles(p1) and "Mt Vernon hours" not in _titles(p1)
    # FIXED (retrieval-precision follow-up): the spoken hours answer now DOES differ by store.
    assert p1.answer != t1.answer
    assert "9 AM" in p1.answer and "10 PM" in p1.answer, "Pullman's own hours row is now spoken"

    p2 = pu.say(SCRIPT[1])
    # Both decline now (no lexical bridge to either store's address row) — the fallback text is
    # store-independent, so they're still byte-identical, just for a different (safer) reason.
    assert p2.answer == t2.answer
    assert p2.grounded is False
    assert PULLMAN_ADDRESS not in p2.answer

    p3 = pu.say(SCRIPT[2])
    assert PULLMAN_ADDRESS in p3.answer
    assert MV_ADDRESS not in p3.answer and YAKIMA_ADDRESS not in p3.answer
    assert p3.answer != t3.answer, "the plain address ask is where store scoping really works"

    p4 = pu.say(SCRIPT[3])
    assert PULLMAN_PHONE in p4.answer and MV_PHONE not in p4.answer

    p5 = pu.say(SCRIPT[4])
    assert p5.answer == t5.answer, "GAP: specials are byte-identical for both stores"

    p6 = pu.say(SCRIPT[5])
    assert p6.args("suggest_products")["store"] == "pullman"
    assert fake_bt.calls["search"][-1]["location"] == "pullman"
    pu_wyld = next(p for p in p6.picks if p["sku"] == "ED-WYLD-10")
    assert pu_wyld["price_otd"] == 15.0, "same SKU, same menu price"
    assert pu_wyld["price_otd"] == mv_wyld["price_otd"], (
        "tax-inclusive pricing is an account-level Dutchie setting, not a per-store rate — no "
        "longer store-scoped"
    )

    assert [call["location"] for call in fake_bt.calls["search"]] == ["mount-vernon", "pullman"]
    assert len(pu.turns) == 6


@pytest.mark.django_db
def test_mt_vernon_alias_is_the_same_store(convo, fake_bt):
    """Dana's handset says "mt vernon"; that has to be the Mount Vernon call, turn for turn."""
    alias = convo(store="mt vernon")
    canonical = convo(store="mount-vernon")

    for message in SCRIPT[:5]:
        a, c = alias.say(message), canonical.say(message)
        assert a.args("faq_lookup")["store"] == "mount-vernon", f"alias lost on {message!r}"
        assert a.result("faq_lookup")["store"] == "mount-vernon"
        assert a.raw["store"] == "mount-vernon"
        assert a.answer == c.answer, f"alias answered differently on {message!r}"
        assert a.intent == c.intent

    assert MV_ADDRESS in alias.turns[2].answer
    assert MV_PHONE in alias.turns[3].answer
    assert len(alias.turns) == 5 and len(canonical.turns) == 5

    # The product turn too — the alias must reach budtender as the canonical slug.
    a6, c6 = alias.say(SCRIPT[5]), canonical.say(SCRIPT[5])
    assert a6.args("suggest_products")["store"] == "mount-vernon"
    assert [call["location"] for call in fake_bt.calls["search"]] == ["mount-vernon", "mount-vernon"]
    assert a6.pick_names == c6.pick_names
    assert a6.picks[0]["price_otd"] == c6.picks[0]["price_otd"]

    # The other spellings a caller or handset can produce land on the same store.
    for spelling in ("mt-vernon", "Mt Vernon", "  MOUNT-VERNON  "):
        t = convo(store=spelling).say(SCRIPT[2])
        assert t.args("faq_lookup")["store"] == "mount-vernon", f"{spelling!r} was not normalized"
        assert MV_ADDRESS in t.answer


@pytest.mark.django_db
def test_a_store_we_dont_have_is_dropped_not_passed_through(convo, fake_bt):
    """Dana asks about Spokane — we have no Spokane, so the slug must never reach a tool."""
    c = convo(store="spokane")

    t1 = c.say(SCRIPT[0])
    assert t1.args("faq_lookup")["store"] == "", "_safe_store must drop an unknown store"
    assert t1.result("faq_lookup")["store"] == ""
    assert t1.raw["store"] == ""
    assert t1.raw["contact_hint"] is None, "no store and no phone → nothing to hand staff"
    # GAP (still present, changed shape): dropping the store changes nothing about WHICH store's
    # hours get spoken — Dana still hears one arbitrary store's clock time. FIXED in kind (retrieval-
    # precision follow-up): topic="hours_location" now excludes the wrong global Yakima-quoting FAQ,
    # so the arbitrary pick is at least a REAL, single-store hours row instead of a mismatched one.
    assert "8AM to 11:30PM" not in t1.answer, "no longer the mismatched global Yakima-quoting FAQ"
    assert "Mt Vernon hours" in _titles(t1)

    t2 = c.say(SCRIPT[2])
    assert t2.args("faq_lookup")["store"] == ""
    # GAP: with no store to scope by, all three address rows compete and the agent speaks whichever
    # wins the tiebreak, verbatim, as if it were Dana's store — Pullman's, right after it cited
    # Mount Vernon's hours row. No hedge, no "which of our three locations?".
    assert set(_titles(t2)) == {"Mt Vernon address", "Yakima address", "Pullman address"}
    assert t2.answer.startswith("pullman Pullman address:")
    assert PULLMAN_ADDRESS in t2.answer

    t3 = c.say(SCRIPT[3])
    assert t3.args("faq_lookup")["store"] == ""
    assert PULLMAN_PHONE in t3.answer

    t4 = c.say(SCRIPT[5])
    assert t4.intent == "product_suggestion"
    assert t4.args("suggest_products")["store"] == "", "the rejected store stays empty in the args"
    # GAP: suggest_products then silently substitutes its own default store, so Dana is quoted
    # Yakima's shelf and Yakima's tax without ever being told which store answered.
    assert fake_bt.calls["search"][-1]["location"] == "yakima"
    assert fake_bt.calls["search"][-1]["slots"]["store"] == "yakima"
    assert next(p for p in t4.picks if p["sku"] == "ED-WYLD-10")["price_otd"] == 15.0
    assert t4.raw["store"] == ""

    for turn in c.turns:
        assert "spokane" not in str(turn.args("faq_lookup").get("store", "")).lower()
        assert "spokane" not in str(turn.args("suggest_products").get("store", "")).lower()
    assert not any("spokane" in str(call["location"]).lower() for call in fake_bt.calls["search"])
    assert len(c.turns) == 4
