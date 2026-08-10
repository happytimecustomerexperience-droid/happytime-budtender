"""Thread 07 — Marcus orders ahead for pickup: the brain picks the items, then stages a cart.

FIXED 2026-08-10: ``answer_text_chat`` had no ``stage_phone_cart`` branch at all, so a staging
("set that aside", "hold it for me", "put me down for it") ask was answered with irrelevant
*online*-order hold copy and nothing was ever staged. It now detects a staging request and calls
``stage_phone_cart`` itself — conservatively: the SKU comes ONLY from the caller's own most
recently suggested pick (``VoiceCall.suggested_skus``, the same durable field ``suggest.py``
already stamps, keyed on the session_token text chat reuses as its call_id — see
``voice/chat.py``'s ``_last_suggested_sku``). Web chat has no other "current product" concept, so
when nothing has been suggested yet there is nothing to stage, and the agent says so honestly
instead of guessing (see ``test_staging_with_no_prior_pick_is_honest_about_it`` below).
"""

from __future__ import annotations

import pytest

from voice.tools import TOOL_REGISTRY, dispatch


@pytest.mark.django_db
def test_order_ahead_for_pickup_now_reaches_the_phone_cart(convo, fake_bt):
    """Six turns of a real order-ahead call: FAQ -> two product picks -> two staging asks that now
    both actually stage something, with one status question in between that still correctly
    defers to a human (it is a QUESTION about a hold, not a request to make one)."""
    c = convo(store="yakima", phone="+15095551234")

    # 1. The opener that makes this an order-ahead call. Unchanged.
    t = c.say("hi there, can I order ahead and pick everything up this afternoon")
    assert t.intent == "general_faq"
    assert t.grounded and t.tools == ["faq_lookup"]
    assert "reserve it for pickup" in t.answer, "must be the online-order KB row, not an invention"
    assert t.sources[0]["title"] == "How do I order online? / Can I order ahead?"

    # 2. First item — the router has to carry size + budget + effect into the search. Unchanged.
    t = c.say("perfect. let's start with a full gram cart, something with energy for daytime, under $40")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args["category"] == "cartridge"
    assert args["size"] == "1g"
    assert args["price_max"] == 40.0
    assert args["effect_desired"] == "uplifted"
    assert t.pick_names == ["Jetty Blue Dream 1g Cart"], "1g + under $40 leaves exactly one cart"
    cart_pick = t.picks[0]
    assert cart_pick["sku"] == "CT-JETTY-1G"
    assert cart_pick["price_otd"] == 35.0, "spoken price is the menu price — tax-inclusive Dutchie account"
    assert "cost" not in cart_pick and "margin" not in cart_pick
    assert "Jetty Jetty Blue Dream" not in t.answer
    assert "the Jetty Blue Dream 1g Cart" in t.answer

    # 3. Second item, named by product — the caller says "raspberry gummies", the router only hears
    #    a category, so the cheapest edible (a beverage) comes back on top. Unchanged.
    t = c.say("nice. add a couple of those raspberry gummies for my wife too")
    assert t.intent == "product_suggestion"
    edible_args = t.args("suggest_products")
    assert edible_args["category"] == "edible"
    assert "quantity" not in edible_args, "'a couple' is dropped — there is no quantity slot"
    assert t.pick_names == ["Cannaquench Sparkling 5mg", "Wyld Raspberry Gummies 10mg"]
    assert t.picks[0]["name"] != "Wyld Raspberry Gummies 10mg", (
        "the product the caller named by name is not the top pick — no name matching in chat.py"
    )

    # 4. The staging ask. FIXED: this now actually stages something.
    t = c.say("great, can you set those two aside under the name Marcus so they're ready when I get there")
    assert "stage_phone_cart" in t.tools, "the staging phrase now reaches stage_phone_cart"
    assert t.intent == "phone_cart_staged"
    assert t.grounded is True
    assert not t.escalated
    assert t.next_action == "answer"
    # Conservative SKU resolution (see module docstring): only the caller's most RECENTLY
    # suggested SKU is ever staged — here, the last item appended to VoiceCall.suggested_skus,
    # which is turn 3's second (lower-ranked) pick, not necessarily "the" top pick. It is always a
    # real item the caller was just shown, never invented, and the answer names it explicitly so
    # the caller can correct it on the spot.
    staged_args = t.args("stage_phone_cart")
    assert staged_args == {"action": "add_item", "store": "yakima", "sku": "ED-WYLD-10", "quantity": 1}
    assert t.result("stage_phone_cart")["ok"] is True
    assert "Wyld Raspberry Gummies 10mg" in t.answer, "the agent names exactly which item it staged"
    assert "Marcus" not in t.answer, (
        "the pickup name still has no slot in chat.py's router — unaddressed by this fix, unchanged"
    )
    # stage_phone_cart takes NO phone argument by design (voice/tools/phone_cart.py injects it
    # server-side from ctx) — confirm that contract held.
    assert "phone" not in staged_args

    # 5. The caller asks point blank whether anything is being held — a QUESTION, not a request,
    #    so the staging gate correctly does not fire (note the past tense "held", never matched by
    #    the staging regex's literal "hold"). Unchanged: the relevance floor declines rather than
    #    guess with the unrelated loyalty-rewards row.
    t = c.say("so is anything actually being held for me right now, or do I have to redo it on the website")
    assert t.tools == ["faq_lookup"]
    assert "stage_phone_cart" not in t.tools
    assert t.grounded is False
    assert "hold" not in t.answer.lower() and "staged" not in t.answer.lower()

    # 6. One last try, phrased as a commitment to pay at the counter. FIXED: "put me down for" is
    #    itself a staging phrase, so this now also stages (the same most-recently-suggested SKU —
    #    nothing changed it between turns 4 and 6).
    t = c.say("okay just put me down for the two of them and I'll pay when I show up")
    assert "stage_phone_cart" in t.tools
    assert t.intent == "phone_cart_staged"
    assert t.grounded is True
    assert t.next_action == "answer"
    assert t.args("stage_phone_cart")["sku"] == "ED-WYLD-10"

    # ── the whole call, in aggregate ────────────────────────────────────────────
    assert len(c.turns) == 6
    assert len(fake_bt.calls.get("phone_cart_upsert", [])) == 2, "turns 4 and 6 both staged"
    for upsert in fake_bt.calls["phone_cart_upsert"]:
        assert upsert["sku"] == "ED-WYLD-10"
        assert upsert["phone"] == "+15095551234"
        assert upsert["audit"]["source"] == "voice_tool"
    assert "phone_cart_release" not in fake_bt.calls
    assert "phone_cart_claim" not in fake_bt.calls
    # check_sku shows up now too — the honest "which item did I just stage" name lookup.
    assert sorted(fake_bt.calls) == ["check_sku", "phone_cart_upsert", "resume_by_phone", "search"]
    assert [call["slots"]["category"] for call in fake_bt.calls["search"]] == ["cartridge", "edible"]
    assert [t.tools for t in c.turns] == [
        ["faq_lookup"],
        ["faq_lookup", "suggest_products"],
        ["faq_lookup", "suggest_products"],
        ["faq_lookup", "stage_phone_cart"],
        ["faq_lookup"],
        ["faq_lookup", "stage_phone_cart"],
    ]

    from voice.models import VoiceCall

    call = VoiceCall.objects.get(call_id=c.session_token)
    assert call.suggested_skus == ["CT-JETTY-1G", "ED-CQ-5", "ED-WYLD-10"], (
        "the durable field _last_suggested_sku reads from — proof the mechanism used the caller's "
        "OWN suggestion history, not an invented sku"
    )


@pytest.mark.django_db
def test_staging_with_no_prior_pick_is_honest_about_it(convo, fake_bt):
    """CRITICAL CONSTRAINT: when nothing has been suggested yet this session, there is no SKU to
    conservatively resolve — the agent must say so and offer a human, never stage a guessed item."""
    c = convo(store="yakima", phone="+15095551234")
    t = c.say("hi, can you set something aside for me")
    assert t.intent == "phone_cart_staged"
    assert "stage_phone_cart" not in t.tools, "no sku resolvable -> no tool call is even attempted"
    assert t.grounded is False
    assert t.next_action == "ask_staff"
    assert "phone_cart_upsert" not in fake_bt.calls
    assert "aside" in t.answer.lower() or "hold" in t.answer.lower()
    assert "team" in t.answer.lower(), "offers a human instead of inventing a sku"


@pytest.mark.django_db
def test_the_staging_tool_works_when_something_actually_calls_it(convo, fake_bt):
    """The tool itself has always worked correctly — hand ``stage_phone_cart`` the SKU a call just
    picked and the draft stages fine. FIXED 2026-08-10: this capability is no longer stranded —
    ``answer_text_chat``'s new staging gate (see ``test_order_ahead_for_pickup_now_reaches_the_
    phone_cart`` above) makes this exact call itself, automatically, from a staging phrase."""
    c = convo(store="yakima", phone="+15095551234")
    t = c.say("I want a full gram cart under $40, something uplifting for the daytime")
    sku = t.picks[0]["sku"]
    assert sku == "CT-JETTY-1G"
    assert "phone_cart_upsert" not in fake_bt.calls, "this particular (non-staging) turn staged nothing"

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
    keeps only the category/size/price the slot extractor knows about. Unchanged: this line names
    no staging phrase ("for pickup" is ordering language, not "set aside"/"hold ... for me"/"put
    me down for"), so it stays a plain product ask."""
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
    assert "stage_phone_cart" not in t.tools
    assert fake_bt.calls["search"][0]["location"] == "mount-vernon"
