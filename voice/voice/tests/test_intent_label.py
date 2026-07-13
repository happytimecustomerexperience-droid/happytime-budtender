"""The router self-labels every turn with a coarse `intent` so sibling services
can classify + track conversations without re-deriving the route. Offline: the
tool layer is faked so only the labelling logic is under test."""
from __future__ import annotations

import pytest

from voice import chat


def _fake_dispatch(grounded_topics):
    """faq_lookup grounds for the listed topic words; suggest_products returns a
    pick only for real category asks."""
    def _dispatch(tool, args, ctx):
        if tool == "faq_lookup":
            q = str(args.get("query") or "").lower()
            if any(t in q for t in grounded_topics):
                return {"grounded": True, "answer": "Grounded answer under WAC 314-55-079.",
                        "sources": [{"kind": "faq", "title": "FAQ", "source_url": "https://happytimeweed.com/faq"}]}
            return {"grounded": False, "fallback": "can't confirm"}
        if tool == "suggest_products":
            return {"picks": [{"name": "Blue Dream 3.5g", "why_this": "matches"}], "spoken_summary": "Found a few."}
        return {}
    return _dispatch


@pytest.mark.parametrize("message,grounded_topics,expected", [
    ("what is your return policy", ["return", "policy"], "return_policy"),
    ("any specials or deals today", ["special", "deal"], "specials"),
    ("what are your hours and address", ["hour", "address"], "hours_location"),
    ("do you have any flower under $30", [], "product_suggestion"),
    ("looking for an indica cart", [], "product_suggestion"),
    ("my vape cart is defective and I want a refund", ["return"], "conflict_resolution"),
    ("this is unacceptable, I feel ripped off", [], "conflict_resolution"),
    ("hey there", [], "greeting_other"),
    ("do you offer delivery", ["delivery"], "general_faq"),
])
def test_answer_text_chat_labels_intent(monkeypatch, message, grounded_topics, expected):
    monkeypatch.setattr(chat, "dispatch", _fake_dispatch(grounded_topics))
    out = chat.answer_text_chat({"message": message, "store": "yakima"})
    assert out["intent"] == expected
