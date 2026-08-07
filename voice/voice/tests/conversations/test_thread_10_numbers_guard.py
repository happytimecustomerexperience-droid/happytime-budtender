"""Thread 10 — Marco, the caller who keeps demanding exact figures: proves the Numbers-Guard, i.e. every number the agent speaks came from a KB row or a live tool result, and the rest is an honest "I can't confirm" that offers a human."""

from __future__ import annotations

import re

import pytest

from voice import pricing

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    """Every numeric token spoken in a line — the thing the Numbers-Guard is about."""
    return set(_NUM_RE.findall(text or ""))


def _tool_numbers(picks: list[dict]) -> set[str]:
    """Every number the budtender tool actually handed back for those picks."""
    out: set[str] = set()
    for pick in picks:
        for key in ("name", "brand", "strain", "why_this", "price_spoken", "price_otd", "thc_percent"):
            out |= _numbers(str(pick.get(key) or ""))
    return out


@pytest.mark.django_db
def test_caller_presses_for_exact_numbers(convo, fake_bt):
    """Six turns of a caller squeezing for potency, an out-the-door total, and a unit count."""
    c = convo(store="yakima")

    # 1. A concrete, answerable ask — this is where a real figure is legitimately available.
    t = c.say("hi there, I'm after a full gram cartridge and I'd like to stay under $40")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["size"] == "1g"
    assert args["price_max"] == 40.0
    assert t.pick_names == ["Jetty Blue Dream 1g Cart"]
    jetty = t.picks[0]
    # The spoken figure is the TOOL's number: budtender's pre-tax 35.00 uplifted by the Yakima
    # OTD multiplier — nothing composed in prose.
    assert jetty["price_otd"] == pricing.otd(35.0, "yakima") == 51.98
    assert jetty["price_spoken"] == "51 dollars and 98 cents"
    assert jetty["price_spoken"] in t.answer
    assert _numbers(t.answer) <= _tool_numbers(t.picks), t.answer
    # ...and the leak-guard still holds on the row that figure came from.
    assert "cost" not in jetty and "margin" not in jetty
    # FINDING (low): the caller capped the budget at $40 and the agent quotes $51.98 out the door —
    # price_max filters the pre-tax shelf price while the spoken number is the OTD price.
    assert args["price_max"] < jetty["price_otd"]

    # 2. He presses for the exact potency of the item he was just offered.
    t = c.say("what's the exact THC percentage on that Jetty cart")
    assert t.intent == "product_suggestion"
    # FINDING: "that Jetty cart" is not resolved — the router re-searches from scratch, drops the
    # size/budget slots it already had, and leads with a different product.
    assert t.args("suggest_products") == {"category": "cartridge", "store": "yakima"}
    assert t.pick_names[0] == "Avitas GSC 0.5g Cart"
    jetty_again = next(p for p in t.picks if p["sku"] == "CT-JETTY-1G")
    # The potency IS available and it is the catalog's number, not an invented one.
    assert jetty_again["thc_percent"] == 84.0
    assert [p["thc_percent"] for p in t.picks] == [79.5, 84.0, 88.0]
    # FINDING: the spoken line never states a THC figure at all — the number the caller asked for
    # rides along in the structured picks only. Un-spoken is safe; it is still a non-answer.
    assert "84" not in t.answer
    assert "percent" not in t.answer.lower()
    assert _numbers(t.answer) <= _tool_numbers(t.picks), t.answer

    # 3. He drops the product words and asks for a hard total. No category ⇒ the KB route.
    t = c.say("okay but what's my out the door total on the Blue Dream if I grab one")
    assert t.tools == ["faq_lookup"]
    assert t.picks == []
    # The Numbers-Guard core: the spoken line is the retrieved KB row VERBATIM, so no figure can
    # be composed — and this row carries no digits at all.
    assert t.answer == t.result("faq_lookup")["answer"]
    assert not _numbers(t.answer), t.answer
    assert t.sources and t.sources[0]["kind"] == "store_fact"
    # FINDING: grounded=True on a question the KB cannot answer — keyword retrieval hands the
    # retail caller a vendor-receiving row (and speaks its raw chunk prefix, "yakima Yakima ...").
    assert t.grounded is True
    assert t.answer.startswith("yakima Yakima vendor receiving:")

    # 4. Now the unit count: "how many are left".
    t = c.say("come on, how many of those carts are actually left on the shelf right now")
    assert t.intent == "product_suggestion"
    assert t.picks
    # A count is physically unreachable: stock never crosses into the speakable pick shape.
    for pick in t.picks:
        assert "stock_on_hand" not in pick and "qty" not in pick
    assert _numbers(t.answer) <= _tool_numbers(t.picks), t.answer
    # The real counts (25 / 18 / 9 in the fake inventory) are nowhere in the spoken line.
    assert not {"25", "18", "9"} & _numbers(t.answer)

    # 5. He lowballs hard. Nothing matches — the honest miss, with a human offered.
    t = c.say("then what about a cart under 15 bucks")
    assert t.grounded is False
    assert t.picks == []
    assert t.args("suggest_products")["price_max"] == 15.0
    assert fake_bt.calls["search"][-1]["slots"]["price_max"] == 15.0
    assert t.next_action == "ask_staff"
    assert "my team" in t.answer
    assert not _numbers(t.answer), t.answer

    # 6. He gives up on the bot and asks for a person to quote him.
    t = c.say("fine, can I just talk to a person who can quote me the exact price")
    assert t.intent == "conflict_resolution"
    assert t.escalated is True
    assert t.next_action == "escalate"
    kb_answer = t.result("faq_lookup")["answer"]
    assert t.answer == f"I'm sorry that happened. {kb_answer} Please share your details for the yakima team so they can contact you at a callback number or email."
    # The escalation wrapper introduces no figure of its own — every number is the KB row's.
    assert _numbers(t.answer) <= _numbers(kb_answer), t.answer
    # FINDING (low): the wrapper apologises ("I'm sorry that happened") to a caller who reported no
    # problem, and speaks the store slug "yakima" rather than "Yakima".
    assert t.answer.startswith("I'm sorry that happened.")
    assert "the yakima team" in t.answer

    assert len(c.turns) == 6
    assert len(fake_bt.calls["search"]) == 4  # turns 3 and 6 never reached inventory


@pytest.mark.django_db
def test_price_tracks_the_tool_and_goes_quiet_when_it_dies(convo, fake_bt):
    """The quoted figure follows live inventory, and when inventory dies the agent quotes nothing."""
    c = convo(store="yakima")

    t = c.say("how much is a full gram cartridge out the door")
    first = next(p for p in t.picks if p["sku"] == "CT-JETTY-1G")
    assert first["price_otd"] == pricing.otd(35.0, "yakima") == 51.98

    # The shelf price is corrected mid-call.
    for row in fake_bt.catalog:
        if row["sku"] == "CT-JETTY-1G":
            row["price"] = 41.0

    t = c.say("my buddy says the Jetty cart went up — what is it now")
    second = next(p for p in t.picks if p["sku"] == "CT-JETTY-1G")
    assert second["price_otd"] == pricing.otd(41.0, "yakima") == 60.89
    assert second["price_spoken"] == "60 dollars and 89 cents"
    # The stale figure is gone everywhere — the agent re-read the tool instead of remembering.
    assert "51 dollars and 98 cents" not in t.answer
    assert "51.98" not in str(t.picks)
    assert _numbers(t.answer) <= _tool_numbers(t.picks), t.answer
    # FINDING: he named a product; the spoken line answers about a different one entirely.
    assert "Jetty" not in t.answer
    assert t.pick_names[0] == "Avitas GSC 0.5g Cart"

    # Inventory goes dark. He pushes for a ballpark anyway.
    fake_bt.fail_search = True
    t = c.say("okay just ballpark it for me, roughly what do your carts run")
    assert t.grounded is False
    assert t.picks == []
    assert t.next_action == "ask_staff"
    assert "my team" in t.answer
    # Not one digit — no ballpark, no remembered price, no invented range.
    assert not _numbers(t.answer), t.answer

    assert len(fake_bt.calls["search"]) == 3  # every ask re-queried the tool
    assert all(call["slots"]["category"] == "cartridge" for call in fake_bt.calls["search"])
    assert all(call["location"] == "yakima" for call in fake_bt.calls["search"])
