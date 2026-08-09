"""Thread 08 — the nervous first-time edible buyer: what the router does with "how much is too much".

Proves the three routes this call needs (low-dose product sizing, the KB dosing/education answer,
and the honest miss) and pins the places the router serves the pitch before the guidance.
"""

from __future__ import annotations

import re

import pytest

from voice.chat import _category_from_text, _size_from_text

# Any "5 mg" / "2.5mg" / "100 mg" figure the agent speaks.
_MG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*mg", re.I)


def _mg_figures(text: str) -> set[str]:
    """Normalized milligram figures in a spoken answer: "2.5 mg" and "2.5mg" both → "2.5mg"."""
    return {f"{m}mg" for m in _MG_RE.findall(text or "")}


def _kb_mg_figures() -> set[str]:
    """Every milligram figure that actually exists in the seeded KB corpus."""
    from kb import models as m

    figures: set[str] = set()
    for model in (m.FAQEntry, m.PolicyDocument, m.StoreFact, m.EducationDoc, m.BlogDoc,
                  m.WeightTypeTaxonomy):
        for row in model.objects.filter(is_active=True):
            figures |= _mg_figures(row.chunk_text())
    return figures


@pytest.mark.django_db
def test_first_timer_asks_how_much_is_too_much(convo, fake_bt):
    """One continuous call: nervous opener → dosing question → 5mg → 10mg on a budget → a miss."""
    c = convo(store="yakima")

    # ── 1. The opener is a question about safety, not a shopping list. ───────────────
    t1 = c.say("hi, I have never tried an edible before and I am nervous about taking too much")
    # FINDING: the bare word "edible" is enough to make `_prefers_products` win, so the router
    # skips its own grounded dosing rows and opens with a sales pitch.
    assert t1.intent == "product_suggestion"
    assert t1.tools == ["faq_lookup", "suggest_products"]
    assert t1.args("suggest_products") == {"category": "edible", "store": "yakima"}
    # "nervous about taking too much" carries no derived dose/effect signal at all.
    assert "size" not in t1.args("suggest_products")
    assert "effect_desired" not in t1.args("suggest_products")
    assert t1.picks and t1.next_action == "show_products"
    assert "top pick" in t1.answer.lower()
    # The KB's beginner dose (2.5 mg) never reaches the caller on the turn they said they were scared.
    assert "2.5" not in t1.answer

    # ── 2. They stop the pitch and ask the actual question. ─────────────────────────
    t2 = c.say("hold on, what dose should I start with")
    assert t2.tools == ["faq_lookup"], "a dosing question must not trigger a product search"
    assert t2.grounded and t2.sources
    assert t2.sources[0]["kind"] == "taxonomy"
    assert t2.sources[0]["title"] == "beginner start"
    assert "2.5 mg" in t2.answer and "10 mg" in t2.answer
    # FINDING: there is no education/dosing intent label — the KB's own dosing row lands in the
    # catch-all bucket, so this turn is indistinguishable from small talk downstream.
    assert t2.intent == "greeting_other"

    # ── 3. They act on that guidance and ask for the small one. ─────────────────────
    t3 = c.say("is a 5 mg gummy a reasonable place to start")
    args3 = t3.args("suggest_products")
    assert args3["size"] == "5mg", "_size_from_text must fire on the spaced '5 mg' phrasing"
    assert args3["category"] == "edible"
    assert fake_bt.calls["search"][-1]["slots"]["size"] == "5mg", "the size slot must reach search"
    assert t3.pick_names == ["Cannaquench Sparkling 5mg"]
    assert "Wyld Raspberry Gummies 10mg" not in t3.pick_names, "the 10mg item must be filtered out"

    # ── 4. Braver now, and watching the money. ──────────────────────────────────────
    t4 = c.say("what about a 10mg gummy under $20")
    args4 = t4.args("suggest_products")
    assert args4["size"] == "10mg", "_size_from_text must fire on the unspaced '10mg' phrasing"
    assert args4["price_max"] == 20.0
    assert t4.pick_names == ["Wyld Raspberry Gummies 10mg"]
    assert t4.pick_names != t3.pick_names, "the new size slot must change the pick"
    # otd() is identity now (tax-inclusive Dutchie account), so the budget filter and the spoken
    # price agree: a caller who said "under $20" is read a number at or under $20.
    pick = t4.picks[0]
    assert pick["price_otd"] <= args4["price_max"]
    assert pick["price_spoken"] == "15 dollars"

    # ── 5. They quote the KB's own wording back at the agent. ───────────────────────
    t5 = c.say("you said a quarter of a 10 mg gummy, do you have anything like that")
    # FINDING: "quarter" hits the 7g flower-weight alias before the mg aliases are tried, so an
    # edible search goes out carrying a flower weight and can never match.
    assert t5.args("suggest_products")["size"] == "7g"
    assert t5.args("suggest_products")["category"] == "edible"
    assert t5.picks == []
    assert t5.grounded is False
    assert t5.next_action == "ask_staff"
    # The miss stays honest — no milligram figure is conjured to fill the silence.
    assert _mg_figures(t5.answer) == set()

    # ── 6. The last worry of every first-timer. ─────────────────────────────────────
    # FIXED (retrieval-precision follow-up): onset time used to be answered, confidently and
    # "grounded", with the unrelated order-pickup ETA row (the dosing row that does answer it was
    # retrieved but ranked below and never spoken). The relevance floor now declines instead of
    # guessing — still honest, though ideally the dosing row would rank first instead (out of
    # scope: that is a ranking-quality gap, not a false-confidence one).
    t6 = c.say("and how long before I feel it")
    assert t6.grounded is False
    assert _mg_figures(t6.answer) == set()

    assert len(c.turns) == 6
    assert c.transcript.count("user:") == 6


@pytest.mark.django_db
def test_no_milligram_figure_is_invented(convo, fake_bt):
    """Every mg the agent speaks traces to a seeded KB row or a real catalog item."""
    c = convo(store="pullman")

    t1 = c.say("what is a microdose")
    assert t1.grounded
    assert t1.sources[0]["kind"] == "taxonomy"
    assert t1.sources[0]["title"] == "microdose"
    assert "2.5 mg" in t1.answer

    t2 = c.say("so what dose should I start with")
    assert t2.grounded
    assert "2.5 mg" in t2.answer

    # Inventory goes dark mid-call; the honest-empty path must not fill the gap with a number.
    fake_bt.fail_search = True
    t3 = c.say("ok show me a 5 mg gummy then")
    assert t3.args("suggest_products")["size"] == "5mg"
    assert t3.picks == []
    assert t3.grounded is False
    assert t3.next_action == "ask_staff"
    assert _mg_figures(t3.answer) == set()

    spoken: set[str] = set()
    for turn in c.turns:
        spoken |= _mg_figures(turn.answer)
    assert spoken, "the thread must actually speak milligram figures or this proves nothing"

    catalog_figures: set[str] = set()
    for row in fake_bt.catalog:
        catalog_figures |= _mg_figures(row["name"]) | _mg_figures(row["size"])
    allowed = _kb_mg_figures() | catalog_figures
    assert spoken <= allowed, f"invented milligram figures: {sorted(spoken - allowed)}"
    assert "2.5mg" in spoken and "2.5mg" in _kb_mg_figures()


@pytest.mark.django_db
def test_dose_size_and_category_parsing():
    """The slot parsers a first-timer's phrasing depends on, including where they mis-fire."""
    # The two sizes this thread rides on, spaced and unspaced.
    assert _size_from_text("10mg") == "10mg"
    assert _size_from_text("do you have a 10 mg gummy") == "10mg"
    assert _size_from_text("5mg") == "5mg"
    assert _size_from_text("something around 5 mg would be good") == "5mg"
    assert _size_from_text("a 20 mg one") == "20mg+"

    # FINDING: the "5mg" alias has no left-hand decimal guard, so the KB's own beginner dose
    # parses as double it — a nervous caller asking for 2.5 mg is searched for 5 mg product.
    assert _size_from_text("I want to start at 2.5 mg") == "5mg"
    # FINDING: the KB's microdose floor has no slot at all.
    assert _size_from_text("can I get 1 mg") == ""
    # FINDING: flower weights are tried before edible doses, so "quarter" beats "10 mg".
    assert _size_from_text("a quarter of a 10 mg gummy") == "7g"

    # Category: the words that route an edible ask, and the ones that silently do not.
    assert _category_from_text("never tried an edible") == "edible"
    assert _category_from_text("a low dose gummy") == "edible"
    assert _category_from_text("10 mg") == "edible"
    # Fixed 2026-08-07: _CATEGORY_RE is now plural-tolerant, so the most natural opener of all
    # (a bare plural, no singular "edible" anywhere in the sentence) routes correctly.
    assert _category_from_text("what edibles do you carry") == "edible"
    # FINDING: unspaced "10mg" carries no category either (the \bmg\b boundary never fires).
    assert _category_from_text("10mg") == ""
