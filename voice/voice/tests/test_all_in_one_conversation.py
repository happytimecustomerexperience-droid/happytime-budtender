"""One conversation, every job: the router must handle greeting, product
suggestion, specials, return policy, AND a defect/refund conflict — all within a
single session — routing each turn correctly. This is the 'does it all in one
conversation' guarantee. Tool layer faked so it runs offline + deterministic."""
from __future__ import annotations

from voice import chat


def _fake_dispatch(tool, args, ctx):
    if tool == "faq_lookup":
        q = str(args.get("query") or "").lower()
        if "special" in q or "deal" in q:
            return {"grounded": True, "answer": "Today: 20% off all vape cartridges.",
                    "sources": [{"kind": "faq", "title": "Specials", "source_url": "https://happytimeweed.com/specials"}]}
        if "return" in q or "policy" in q:
            return {"grounded": True, "answer": "Defective items may be reviewed by staff under WAC 314-55-079.",
                    "sources": [{"kind": "faq", "title": "Return policy", "source_url": "https://happytimeweed.com/faq"}]}
        return {"grounded": False, "fallback": "I can't confirm that."}
    if tool == "suggest_products":
        return {"picks": [{"name": "Blue Dream Cart 1g", "why_this": "indica-leaning, under budget"}],
                "spoken_summary": "Here are a couple of carts that fit."}
    return {}


def _turn(monkeypatch, history, message, store="yakima"):
    monkeypatch.setattr(chat, "dispatch", _fake_dispatch)
    out = chat.answer_text_chat({"message": message, "store": store, "history": history})
    history.append({"role": "user", "content": message})
    history.append({"role": "assistant", "content": out["answer"]})
    return out


def test_single_conversation_handles_every_intent(monkeypatch):
    history: list[dict] = []

    greet = _turn(monkeypatch, history, "hey there, first time here")
    assert greet["intent"] == "greeting_other"

    product = _turn(monkeypatch, history, "looking for an indica cart under $40")
    assert product["intent"] == "product_suggestion"
    assert product["safe_next_action"] == "show_products"
    assert product["tool_results"][-1]["result"]["picks"]

    specials = _turn(monkeypatch, history, "nice — any specials today?")
    assert specials["intent"] == "specials"
    assert specials["grounded"] is True
    assert "20% off" in specials["answer"]

    policy = _turn(monkeypatch, history, "and what is your return policy?")
    assert policy["intent"] == "return_policy"
    assert policy["grounded"] is True
    assert "WAC 314-55-079" in policy["answer"]

    conflict = _turn(monkeypatch, history, "actually the cart I bought is defective and I want a refund")
    assert conflict["intent"] == "conflict_resolution"
    assert conflict["escalation_required"] is True
    assert conflict["safe_next_action"] == "escalate"
    assert "sorry" in conflict["answer"].lower()

    # the full conversation stayed on one session and exercised all five intents.
    assert len(history) == 10
