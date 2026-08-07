"""Thread 07 — Marcus orders ahead for pickup: the brain picks the items, then never stages a cart."""

from __future__ import annotations

import pytest

from voice.tools import TOOL_REGISTRY, dispatch


@pytest.mark.django_db
def test_order_ahead_for_pickup_never_reaches_the_phone_cart(convo, fake_bt):
    """Six turns of a real order-ahead call: FAQ → two product picks → three tries at "hold it for me".

    The picks land. The hold never does: ``answer_text_chat`` only ever dispatches ``faq_lookup``
    and ``suggest_products`` (voice/chat.py has no ``stage_phone_cart`` branch), so the caller's
    "set those aside under Marcus" is answered with *online*-order hold policy and nothing is staged.
    """
    c = convo(store="yakima", phone="+15095551234")

    # 1. The opener that makes this an order-ahead call.
    t = c.say("hi there, can I order ahead and pick everything up this afternoon")
    assert t.intent == "general_faq"
    assert t.grounded and t.tools == ["faq_lookup"]
    assert "reserve it for pickup" in t.answer, "must be the online-order KB row, not an invention"
    assert t.sources[0]["title"] == "How do I order online? / Can I order ahead?"

    # 2. First item — the router has to carry size + budget + effect into the search.
    t = c.say("perfect. let's start with a full gram cart, something with energy for daytime, under $40")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["size"] == "1g"
    assert args["price_max"] == 40.0
    # "energy" → focused (_EFFECT_ALIASES), then mapped to budtender's vocabulary before dispatch
    # (fixed 2026-08-07: _EFFECT_TO_BUDTENDER maps focused -> uplifted).
    assert args["effect_desired"] == "uplifted"
    assert t.pick_names == ["Jetty Blue Dream 1g Cart"], "1g + under $40 leaves exactly one cart"
    cart_pick = t.picks[0]
    assert cart_pick["sku"] == "CT-JETTY-1G"
    assert cart_pick["price_otd"] == 35.0, "spoken price is the menu price — tax-inclusive Dutchie account"
    assert "cost" not in cart_pick and "margin" not in cart_pick
    # Documents the doubled-brand read-back: brand is prefixed onto a name that already carries it.
    assert "Jetty Jetty Blue Dream" in t.answer

    # 3. Second item, named by product — the caller says "raspberry gummies", the router only hears
    #    a category, so the cheapest edible (a beverage) comes back on top.
    t = c.say("nice. add a couple of those raspberry gummies for my wife too")
    assert t.intent == "product_suggestion"
    edible_args = t.args("suggest_products")
    assert edible_args["category"] == "edible"
    assert "quantity" not in edible_args, "'a couple' is dropped — there is no quantity slot"
    assert t.pick_names == ["Cannaquench Sparkling 5mg", "Wyld Raspberry Gummies 10mg"]
    assert t.picks[0]["name"] != "Wyld Raspberry Gummies 10mg", (
        "the product the caller named by name is not the top pick — no name matching in chat.py"
    )

    # 4. The staging ask. This is the turn a phone cart exists for.
    t = c.say("great, can you set those two aside under the name Marcus so they're ready when I get there")
    assert t.tools == ["faq_lookup"], "no stage_phone_cart branch exists in answer_text_chat"
    assert "stage_phone_cart" not in t.tools
    assert t.intent == "greeting_other", "an explicit hold request has no intent label of its own"
    assert t.grounded and t.next_action == "answer"
    assert not t.escalated
    # It answers with the ONLINE reservation hold window — which is about a Dutchie order the caller
    # never placed, not about the two items just picked on this call.
    assert "held until end of business" in t.answer
    assert "Marcus" not in t.answer, "the pickup name is never captured or read back"

    # 5. The caller asks point blank whether anything is being held. Grounded — at the loyalty row.
    t = c.say("so is anything actually being held for me right now, or do I have to redo it on the website")
    assert t.tools == ["faq_lookup"]
    assert t.grounded is True
    assert "loyalty" in t.sources[0]["title"].lower(), "keyword retrieval answers a hold question with rewards"
    assert "hold" not in t.answer.lower() and "staged" not in t.answer.lower()
    assert t.next_action == "answer", "a confidently-wrong grounded answer never offers a human"

    # 6. One last try, phrased as a commitment to pay at the counter. Still nothing staged.
    t = c.say("okay just put me down for the two of them and I'll pay when I show up")
    assert t.tools == ["faq_lookup"]
    assert t.intent == "greeting_other"
    assert not t.escalated and t.next_action == "answer"

    # The whole point of the thread: across six turns and two product picks, the phone cart was
    # never touched. Only search + recognition ever reached the budtender client.
    assert len(c.turns) == 6
    assert "phone_cart_upsert" not in fake_bt.calls
    assert "phone_cart_release" not in fake_bt.calls
    assert "phone_cart_claim" not in fake_bt.calls
    assert sorted(fake_bt.calls) == ["resume_by_phone", "search"]
    assert [call["slots"]["category"] for call in fake_bt.calls["search"]] == ["cartridge", "edible"]
    assert all("stage_phone_cart" not in turn.tools for turn in c.turns)


@pytest.mark.django_db
def test_the_staging_tool_works_when_something_actually_calls_it(convo, fake_bt):
    """The gap is routing, not a broken tool: hand ``stage_phone_cart`` the SKU the call just picked
    and the draft stages fine — the shared text brain simply never makes that call itself."""
    c = convo(store="yakima", phone="+15095551234")
    t = c.say("I want a full gram cart under $40, something uplifting for the daytime")
    sku = t.picks[0]["sku"]
    assert sku == "CT-JETTY-1G"
    assert "phone_cart_upsert" not in fake_bt.calls, "the conversation itself staged nothing"

    # The tool the budtender persona is told to use (seed.py: role "budtender" tool_names) is
    # registered and reachable — the Vapi LLM can call it; answer_text_chat has no path to.
    assert "stage_phone_cart" in TOOL_REGISTRY
    staged = dispatch(
        "stage_phone_cart",
        {"action": "add_item", "store": "yakima", "sku": sku, "quantity": 2},
        {"store": "yakima", "session_token": "convo-test", "_caller_phone": "+15095551234"},
    )
    assert staged["ok"] is True
    assert "staged cart" in staged["spoken_summary"]

    upserts = fake_bt.calls["phone_cart_upsert"]
    assert len(upserts) == 1
    assert upserts[0]["sku"] == "CT-JETTY-1G"
    assert upserts[0]["quantity"] == 2
    assert upserts[0]["phone"] == "+15095551234"
    assert upserts[0]["audit"]["source"] == "voice_tool"


@pytest.mark.django_db
def test_pickup_details_in_one_breath_are_dropped_from_the_derived_slots(convo, fake_bt):
    """A caller who says the whole order in one line — quantity, brand, pickup name, pickup time —
    keeps only the category/size/price the slot extractor knows about."""
    c = convo(store="mount-vernon", phone="+13604885555")
    t = c.say("two of the Jetty half gram carts for pickup under the name Marcus around five")

    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["size"] == "0.5g"
    assert args["store"] == "mount-vernon", "the store rides along so stock is the right shelf"
    for dropped in ("quantity", "pickup_name", "pickup_time", "brand", "name"):
        assert dropped not in args, f"{dropped} has no slot in chat.py's product router"

    assert t.pick_names == ["Avitas GSC 0.5g Cart"]
    assert t.next_action == "show_products", "not 'stage_cart' — showing is the only offer it makes"
    assert "phone_cart_upsert" not in fake_bt.calls
    assert fake_bt.calls["search"][0]["location"] == "mount-vernon"
