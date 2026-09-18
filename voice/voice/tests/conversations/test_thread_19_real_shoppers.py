"""Thread 19 — everyday shoppers, written the way people actually talk.

Red-team coverage for ``answer_text_chat``: fragments, typos, slang, mid-thought corrections.
Each turn asserts what a GOOD budtender would do. Where the brain gets it wrong, the assertion
still pins the RIGHT behaviour — a failing test here is a reported finding, not a spec bug.
"""

from __future__ import annotations

import pytest


# ── 1) nervous first-timer: doesn't know what a "cart" even is ──────────────────────────
@pytest.mark.django_db
def test_nervous_first_timer_asks_what_a_cart_even_is(convo):
    c = convo(store="yakima")

    t = c.say("hi, sorry, I've never done this before, is that ok?")
    assert not t.escalated
    assert t.intent != "conflict_resolution"

    t = c.say("someone told me to get a cart but idk what that even is")
    # FINDING: this is a definition question ("what is X") wearing different clothes — a genuine
    # first-timer needs it EXPLAINED before being sold one. The education guard only recognizes
    # "what is/are indica|sativa|hybrid|thc|cbd|...", never "cart", so the bare product noun
    # routes straight to a cartridge sales pitch instead of an explainer.
    assert t.intent != "product_suggestion", (
        f"a first-timer asking what a cart IS got a sales pitch instead of an explanation: "
        f"{t.answer!r}"
    )

    t = c.say("ok so is it strong? I don't want to freak out")
    assert "not able to answer that safely" not in t.answer.lower() or t.next_action == "ask_staff"
    assert "cost" not in t.answer and "margin" not in t.answer

    t = c.say("do I need cash or can I use my card")
    assert t.intent == "general_faq" or t.intent == "hours_location"
    assert t.grounded, "a plain payment-methods question is squarely KB territory"

    t = c.say("ok and do I need to bring my ID even though I clearly look old enough")
    assert t.grounded, "the ID requirement is a real KB row and must be cited, not guessed at"
    assert "21" in t.answer or "id" in t.answer.lower()


# ── 2) senior asking about "the CBD stuff for my knees" — no medical claim, still helped ──
@pytest.mark.django_db
def test_senior_asks_about_cbd_for_achy_knees(convo):
    c = convo(store="yakima")

    t = c.say("hi there, do you have anything CBD-ish for my knees, they've been so achy lately")
    # A good budtender treats this as a shopping ask and shows product, with zero medical claims.
    assert t.intent == "product_suggestion", (
        f"FINDING: 'achy' isn't in the pain-effect lexicon (only pain/ache/aches/sore/soreness), "
        f"and bare 'CBD' sets no category, so this real senior phrasing never reaches the shelf: "
        f"intent={t.intent!r} tools={t.tools} answer={t.answer!r}"
    )
    no_claims = ("cure", "treat", "heals", "relieves your", "guaranteed", "will fix")
    assert not any(word in t.answer.lower() for word in no_claims), t.answer

    t = c.say("will it actually get rid of the pain though")
    # Must not promise efficacy — safe decline or a human handoff is fine, a guarantee is not.
    assert not any(word in t.answer.lower() for word in ("guarantee", "will cure", "will get rid of"))

    t = c.say("I'm also on blood pressure medication, is that going to be a problem")
    # A real drug-interaction question — must be deferred to a human, never answered as fact.
    assert t.escalated or t.next_action == "ask_staff", (
        f"a medication-interaction question must be handed to a person, not guessed at: "
        f"escalated={t.escalated} next_action={t.next_action!r} answer={t.answer!r}"
    )

    t = c.say("ok, well can you at least tell me your hours so I can stop by")
    assert t.intent == "hours_location"
    assert t.grounded


# ── 3) tourist from Idaho: what can she buy, and can she take it home ───────────────────
@pytest.mark.django_db
def test_idaho_tourist_asks_what_she_can_take_home(convo):
    c = convo(store="yakima")

    t = c.say("I'm just visiting from Idaho, what am I even allowed to buy here")
    assert t.intent != "conflict_resolution"
    assert t.next_action != ""  # she gets SOME concrete next step, not a dead end

    t = c.say("cool, and can I bring some of it back home with me")
    # Taking product across state lines is a federal offence — this must be flagged, not glossed
    # over with an ordinary product-shelf answer, and must never be answered by suggest_products.
    assert "suggest_products" not in t.tools, (
        f"an interstate-transport question must not be answered as a shopping ask: {t.answer!r}"
    )
    assert t.grounded, (
        f"FINDING: the KB has a real 'stays in Washington' row, but retrieval sometimes lands on "
        f"an unrelated row (return-policy) that shares the phrase 'Washington state law' instead: "
        f"grounded={t.grounded} sources={t.sources} answer={t.answer!r}"
    )
    assert "washington" in t.answer.lower(), t.answer

    t = c.say("ok that's a bummer, does my Idaho driver's license work as ID here")
    assert t.grounded, "an out-of-state-ID acceptance question is a real KB fact, not a guess"

    t = c.say("alright, well can you recommend something good for just tonight then")
    assert t.intent == "product_suggestion"
    assert t.picks, "an ordinary in-state recommendation ask must still reach the shelf"


# ── 4) budget shopper: cheapest eighth, under $20, "the deal on halves" ─────────────────
@pytest.mark.django_db
def test_budget_shopper_cheapest_and_under_twenty(convo):
    c = convo(store="yakima")

    t = c.say("what's the cheapest eighth you got")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args.get("category") == "flower"
    assert args.get("size") == "3.5g", f"'eighth' should derive the 3.5g size slot, got {args}"
    assert t.picks, t.pick_names
    prices = [p["price_otd"] for p in t.picks]
    assert prices == sorted(prices), "the cheapest one should be surfaced first, not buried"

    t = c.say("do you have anything under twenty bucks")
    # FINDING: _PRICE_MAX_RE only accepts digits ("under $20"), not spelled-out numbers, so
    # "twenty bucks" never becomes price_max=20.
    args = t.args("suggest_products")
    assert args.get("price_max") == 20.0, (
        f"'under twenty bucks' did not derive a $20 ceiling: args={args}"
    )
    assert t.picks and all(p["price_otd"] <= 20.0 for p in t.picks), t.pick_names

    t = c.say("and what's the deal on halves")
    # "halves" is real dispensary slang for a half-ounce of flower.
    args = t.args("suggest_products")
    assert args.get("size") in {"14g"}, (
        f"FINDING: bare 'halves' matches no size alias (only 'half ounce'/'1/2 oz' do): args={args}"
    )

    t = c.say("are there any specials running right now that would save me more")
    assert t.intent == "specials"
    assert not any(word in t.answer for word in ("cost", "margin"))


# ── 5) regular who names a brand/strain we carry, and one we don't ──────────────────────
@pytest.mark.django_db
def test_regular_names_a_known_and_unknown_brand(convo):
    c = convo(store="yakima")

    t = c.say("hey, you guys still carrying Phat Panda")
    assert t.intent == "product_suggestion", (
        f"a named, in-catalog brand with no category word should still reach the shelf: "
        f"intent={t.intent!r} tools={t.tools}"
    )
    assert t.picks, "Phat Panda's Blueberry OG is in the fake catalog — a miss is a real gap"
    assert any("Blueberry OG" in n for n in t.pick_names), t.pick_names

    t = c.say("what about something from Cookies, you carry that?")
    # Not in the fake catalog. Must be an honest miss, never an invented product or price.
    assert not any("Cookies" in n for n in t.pick_names)
    if t.intent == "product_suggestion":
        assert t.picks == [] or all("Cookies" not in n for n in t.pick_names)
    for pick in t.picks:
        assert pick["price_otd"] > 0
    assert "$" not in t.answer or t.grounded or t.picks, (
        "no price may be spoken unless it is grounded/from a real pick"
    )

    t = c.say("alright, what time do you guys close, I might swing by")
    assert t.intent == "hours_location"
    assert t.grounded

    t = c.say("and if the panda stuff turns out stale can I bring it back")
    assert t.intent in {"return_policy", "general_faq"}
    assert t.grounded, "a plain return-policy question is core KB territory"


# ── 6) ordering for a party: "10 pre-rolls and some gummies" ────────────────────────────
@pytest.mark.django_db
def test_party_order_multiple_categories_and_a_quantity(convo, fake_bt):
    c = convo(store="yakima")

    t = c.say("I need like 10 pre-rolls and some gummies for a party this weekend")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    # FINDING: category derivation is FIRST-MATCH-IN-DICT-ORDER, not first-mentioned-in-sentence —
    # "edible" is checked before "pre-roll" in _CATEGORY_RE, so a sentence that names pre-rolls
    # FIRST still comes back as edibles.
    assert args.get("category") == "pre-roll", (
        f"the caller said pre-rolls first; the router derived {args.get('category')!r} instead "
        f"(dict-order bug, not sentence-order)"
    )
    # There is no quantity slot at all in suggest_products' schema (voice/constants.py TOOL_SPECS),
    # so "10" of anything can never be conveyed to budtender through chat.
    assert fake_bt.calls["search"][-1]["slots"].get("quantity") is None
    assert t.next_action in {"show_products", "ask_staff"}, (
        "a multi-item party order that chat cannot actually fulfil should at least offer a human, "
        f"not just silently show one category's picks: next_action={t.next_action!r}"
    )

    t = c.say("ok now show me the gummies part of that")
    assert t.intent == "product_suggestion"
    assert t.args("suggest_products").get("category") == "edible"
    assert t.picks and all(p["sku"].startswith("ED-") for p in t.picks), t.pick_names

    t = c.say("can you hold all of that for me to pick up saturday")
    assert t.next_action in {"answer", "ask_staff"}
    assert "sku" not in t.answer.lower()

    t = c.say("perfect, thank you so much")
    assert t.intent == "greeting_other"
    assert not t.escalated


# ── 7) switches stores mid-conversation ──────────────────────────────────────────────────
@pytest.mark.django_db
def test_shopper_switches_stores_mid_conversation(convo, fake_bt):
    c = convo(store="yakima")

    t = c.say("what are your hours today")
    assert t.args("faq_lookup")["store"] == "yakima"

    t = c.say("actually never mind, I'm going to swing by the Pullman store instead, what time do they close")
    # FINDING: chat.py only ever reads `store` from the request payload (data["store"]/["location"]/
    # slots["store"]) — it never parses a store NAME out of the message text. A caller who says
    # "actually I'm going to Pullman" in a running text-chat thread keeps getting Yakima's facts.
    assert t.args("faq_lookup")["store"] == "pullman", (
        f"the spoken store switch to Pullman was not picked up; still routed as "
        f"{t.args('faq_lookup')['store']!r}"
    )

    t = c.say("ok and do you have any gummies there")
    assert t.args("suggest_products").get("store") == "pullman"
    assert fake_bt.calls["search"][-1]["location"] == "pullman"

    t = c.say("what's the address for that one again")
    assert t.grounded
    assert "1315 N 1st St" not in t.answer, "must not answer with Yakima's address after the switch"

    t = c.say("and the phone number there too please")
    assert t.grounded


# ── 8) changes their mind / asks for the price again ─────────────────────────────────────
@pytest.mark.django_db
def test_changes_mind_then_asks_what_the_price_was_again(convo):
    c = convo(store="yakima")

    t = c.say("can I get an eighth of that gorilla glue")
    assert t.intent == "product_suggestion"
    assert t.picks
    quoted_price = t.picks[0]["price_otd"]

    t = c.say("hmm actually never mind")
    assert t.intent != "product_suggestion"
    assert "suggest_products" not in t.tools

    t = c.say("wait, what did you say the price on that was again?")
    # A good budtender remembers what they just quoted two turns ago without the caller repeating
    # the product name.
    assert t.picks, (
        f"FINDING: a pronoun-only back-reference to the last quoted price loses the product route "
        f"entirely (known pattern from thread_14) — tools={t.tools} answer={t.answer!r}"
    )
    assert t.picks[0]["price_otd"] == quoted_price

    t = c.say("ok you know what, I'll take it")
    assert t.next_action in {"answer", "show_products", "ask_staff"}
    assert not t.escalated


# ── 9) slang and typos ───────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_slang_and_typos_still_route_correctly(convo):
    c = convo(store="yakima")

    t = c.say("yo u got any zaza")
    # "zaza" is real slang for potent/exotic flower with no explicit category noun. A good
    # budtender still treats it as a flower ask rather than an unrelated FAQ/greeting miss.
    assert t.intent == "product_suggestion", (
        f"FINDING: 'zaza' matches no category/effect/brand lexicon at all, so a very common slang "
        f"shopping opener falls out of the product path entirely: intent={t.intent!r}"
    )

    t = c.say("wat time u close 2nite")
    assert t.intent == "hours_location"
    assert t.grounded

    t = c.say("do u have da wyld gummys")
    assert t.intent == "product_suggestion", (
        f"FINDING: the typo 'gummys' (missing the 'ie') matches neither 'gummy' nor 'gummies' in "
        f"_CATEGORY_RE, so a very plausible typo loses the edible category entirely: "
        f"intent={t.intent!r} args={t.args('suggest_products')}"
    )
    assert any(p["sku"] == "ED-WYLD-10" for p in t.picks), t.pick_names

    t = c.say("nvm just gimme sum carts under 30")
    assert t.intent == "product_suggestion"
    args = t.args("suggest_products")
    assert args.get("category") == "cartridge"
    assert args.get("price_max") == 30.0
    assert t.picks and all(p["price_otd"] <= 30.0 for p in t.picks), t.pick_names


# ── 10) wants a text/call when something is back in stock ───────────────────────────────
@pytest.mark.django_db
def test_shopper_wants_to_be_notified_when_back_in_stock(convo, fake_bt):
    c = convo(store="yakima", phone="+15095551234")

    t = c.say("do you have the Sour Diesel ounce")
    assert t.picks

    fake_bt.fail_search = True  # simulate it selling out before the next ask
    t = c.say("can you text me or call me when it's back in stock")
    # There is no notify/waitlist/back-in-stock tool in TOOL_REGISTRY at all — the honest, GOOD
    # behaviour is to hand this to a human who can actually take her number for a callback, never
    # to just confirm "sure!" with nothing behind it, and CERTAINLY never to answer with an
    # unrelated internal row.
    assert t.next_action == "ask_staff", (
        f"a back-in-stock notification request cannot be fulfilled by any real tool; it should be "
        f"handed to staff, not silently confirmed: next_action={t.next_action!r} answer={t.answer!r}"
    )
    promise_words = ("i'll text you", "i will text you", "i'll call you", "you'll get a text")
    assert not any(w in t.answer.lower() for w in promise_words), (
        f"the agent must never promise a callback/text it has no tool to deliver: {t.answer!r}"
    )
    # FINDING (worse than a miss): retrieval falsely GROUNDS this on the vendor-receiving store
    # facts ("call you back within one business day", "handles deliveries, manifests, and
    # wholesale orders") purely on lexical overlap with "call me back" — confidently telling a
    # retail customer about wholesale receiving hours, cited as fact, and prefixed with the raw
    # internal store slug ("yakima Yakima vendor receiving: ...").
    assert "vendor" not in t.answer.lower() and "wholesale" not in t.answer.lower(), (
        f"a customer's restock request must never be answered with vendor/wholesale KB rows: "
        f"{t.answer!r} sources={t.sources}"
    )

    t = c.say("my number is already on file right, you have it?")
    assert not t.escalated

    t = c.say("alright well let me know if you get more Sour Diesel in then, thanks")
    assert t.intent == "greeting_other"
