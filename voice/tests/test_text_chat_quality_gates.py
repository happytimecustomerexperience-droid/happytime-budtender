from __future__ import annotations

from voice import chat


def _chat_fn(monkeypatch, tool_results):
    tool_results_map: dict[str, callable] = {}

    def _dispatch(tool, args, ctx):
        tool_results.append((tool, args, dict(ctx)))
        return tool_results_map[tool]()

    def fake_resolve(number, ctx, client=None):
        ctx["recognition_resolved"] = True
        if number:
            ctx["known"] = False
            ctx["_caller_phone"] = number
            ctx["session_token"] = ctx.get("session_token")
        return ctx

    monkeypatch.setattr(chat, "dispatch", _dispatch)
    monkeypatch.setattr("voice.tools.suggest.recognition.resolve_caller", fake_resolve)
    return tool_results_map


def test_conflict_policy_without_source_does_not_claim_grounded(monkeypatch):
    from voice import chat

    tool_results = []
    tool_results_map = _chat_fn(
        monkeypatch,
        tool_results,
    )
    tool_results_map["faq_lookup"] = lambda: {
        "grounded": True,
        "answer": "Returns are handled by staff under WAC 314-55-079.",
        "sources": [],
    }

    out = chat.answer_text_chat(
        {"message": "what is your return policy", "store": "yakima"}
    )

    assert out["grounded"] is False
    assert out["escalation_required"] is False
    assert out["safe_next_action"] == "ask_staff"
    assert "can't confirm" in out["answer"]
    assert out["contact_hint"] == {"store": "yakima", "customer_phone": ""}
    assert "staff" in out["safe_suggested_next_action"]
    assert tool_results[0][0] == "faq_lookup"


def test_conflict_policy_with_source_stays_grounded(monkeypatch):
    from voice import chat

    tool_results = []
    tool_results_map = _chat_fn(
        monkeypatch,
        tool_results,
    )
    tool_results_map["faq_lookup"] = lambda: {
        "grounded": True,
        "answer": "Defective products are reviewed by staff under WAC 314-55-079.",
        "sources": [
            {"source_url": "https://happytimeweed.com/faq", "kind": "kb", "title": "FAQ"}
        ],
    }

    out = chat.answer_text_chat(
        {"message": "what is your return policy", "store": "yakima"}
    )

    assert out["grounded"] is True
    assert out["safe_next_action"] == "answer"
    assert out["sources"][0]["source_url"] == "https://happytimeweed.com/faq"


def test_conflict_escalation_remains_empathic_when_grounded(monkeypatch):
    from voice import chat

    tool_results = []
    tool_results_map = _chat_fn(
        monkeypatch,
        tool_results,
    )
    tool_results_map["faq_lookup"] = lambda: {
        "grounded": True,
        "answer": "Defective products may be reviewed by staff under WAC 314-55-079.",
        "sources": [
            {"source_url": "https://happytimeweed.com/faq", "kind": "kb", "title": "FAQ"}
        ],
    }

    out = chat.answer_text_chat(
        {"message": "this vape cart is defective, please help me", "store": "pullman"}
    )

    assert out["grounded"] is True
    assert out["escalation_required"] is True
    assert out["safe_next_action"] == "escalate"
    assert out["answer"].startswith("I'm sorry that happened.")
    assert "pullman team" in out["answer"].lower()
    assert "staff" in out["safe_suggested_next_action"]
    assert out["contact_hint"] == {"store": "pullman", "customer_phone": ""}


def test_suggestions_forward_exclusions_and_profile_to_tool(monkeypatch):
    from voice import chat

    tool_results = []
    tool_results_map = _chat_fn(
        monkeypatch,
        tool_results,
    )
    tool_results_map["faq_lookup"] = lambda: {"grounded": False, "fallback": "no faq match"}
    tool_results_map["suggest_products"] = lambda: {
        "spoken_summary": "I found three in-stock options with strong match confidence.",
        "picks": [
            {
                "sku": "SKU1",
                "name": "Blue Dream 1g",
                "why_this": "Matches your preferred terpene profile and recent flavor history.",
            },
            {
                "sku": "SKU2",
                "name": "Pineapple Express 1g",
                "why_this": "Strong taste match for recent in-session preferences.",
            },
        ],
    }

    out = chat.answer_text_chat(
        {
            "message": "show me a vape cartridge",
            "store": "yakima",
            "customer_phone": "+1 (509) 555-1212",
            "session_token": "sess-42",
            "exclude_skus": ["OLD2", "OLD3"],
            "slots": {"category": "cartridge"},
        }
    )

    call = tool_results[1]
    assert call[0] == "suggest_products"
    assert call[1]["category"] == "cartridge"
    assert call[1]["exclude_skus"] == ["OLD2", "OLD3"]
    assert call[2]["_caller_phone"] == "+15095551212"
    assert call[2]["session_token"] == "sess-42"
    assert out["safe_next_action"] == "show_products"
    assert "strong match confidence" in out["answer"]


def test_suggestions_resolve_profile_before_rank(monkeypatch):
    from voice import chat

    tool_results = []
    tool_results_map = _chat_fn(monkeypatch, tool_results)
    tool_results_map["faq_lookup"] = lambda: {"grounded": False, "fallback": "no faq match"}
    tool_results_map["suggest_products"] = lambda: {
        "spoken_summary": "I found options based on your profile.",
        "picks": [{"sku": "SKU_PROFILE", "name": "Blue Dream", "why_this": "matches your purchase history"}],
    }

    def fake_resolve(number, ctx, client=None):
        ctx["recognition_resolved"] = True
        ctx["known"] = True
        ctx["session_token"] = "phone-backed-token"
        ctx["profile_summary"] = {"has_history": True, "top_categories": ["vape"], "price_tier": "mid"}
        ctx["caller_phone_hash"] = "k" * 64
        return ctx

    monkeypatch.setattr("voice.tools.suggest.recognition.resolve_caller", fake_resolve)

    out = chat.answer_text_chat(
        {
            "message": "show me flower",
            "store": "yakima",
            "customer_phone": "+15097770011",
            "slots": {"category": "flower"},
        }
    )

    suggest_ctx = tool_results[-1][2]
    assert suggest_ctx["known"] is True
    assert suggest_ctx["session_token"] == "phone-backed-token"
    assert suggest_ctx["caller_phone_hash"] == "k" * 64
    assert out["safe_next_action"] == "show_products"


def test_policy_source_with_empty_answer_drops_grounding(monkeypatch):
    tool_results = []
    tool_results_map = _chat_fn(
        monkeypatch,
        tool_results,
    )
    tool_results_map["faq_lookup"] = lambda: {"grounded": True, "answer": "", "sources": [{"kind": "faq", "title": "FAQ"}]}
    tool_results_map["suggest_products"] = lambda: {"spoken_summary": "I found picks", "picks": []}

    out = chat.answer_text_chat({"message": "what is your return policy", "store": "yakima"})
    assert out["grounded"] is False
    assert out["safe_next_action"] == "ask_staff"
    assert out["safe_next_action"] != "answer"
    assert not out["sources"]


def test_ask_staff_includes_contact_context(monkeypatch):
    tool_results = []
    tool_results_map = _chat_fn(monkeypatch, tool_results)
    tool_results_map["faq_lookup"] = lambda: {"grounded": True, "answer": "Legal policy is documented."}

    out = chat.answer_text_chat(
        {
            "message": "what is your legal policy for returns",
            "store": "pullman",
            "customer_phone": "+1 (509) 555-9999",
        }
    )

    assert out["safe_next_action"] == "ask_staff"
    assert out["contact_hint"]["store"] == "pullman"
    assert out["contact_hint"]["customer_phone"] == "+15095559999"
    assert "team" in out["answer"]


def test_profile_top_category_drives_recommendation(monkeypatch):
    from voice import chat

    tool_results = []
    tool_results_map = _chat_fn(
        monkeypatch,
        tool_results,
    )
    tool_results_map["faq_lookup"] = lambda: {"grounded": False, "fallback": "no faq match"}
    tool_results_map["suggest_products"] = lambda: {
        "spoken_summary": "I found picks from your profile.",
        "picks": [{"sku": "P1", "name": "Blue Dream", "why_this": "matches your habits"}],
    }

    def fake_resolve(number, ctx, client=None):
        ctx["recognition_resolved"] = True
        ctx["known"] = True
        ctx["session_token"] = "phone-backed-token"
        ctx["profile_summary"] = {
            "has_history": True,
            "top_categories": [
                {"category": "flower", "share": 55},
            ],
            "price_tier": "mid",
        }
        ctx["caller_phone_hash"] = "k" * 64
        return ctx

    monkeypatch.setattr("voice.tools.suggest.recognition.resolve_caller", fake_resolve)

    out = chat.answer_text_chat(
        {
            "message": "any recommendations for me",
            "store": "yakima",
            "customer_phone": "+15097770011",
        }
    )

    suggest_call = tool_results[-1]
    assert suggest_call[0] == "suggest_products"
    assert suggest_call[1]["category"] == "flower"
    assert out["safe_next_action"] == "show_products"


def test_profile_top_category_accepts_tuple_profile_summary(monkeypatch):
    from voice import chat

    tool_results = []
    tool_results_map = _chat_fn(
        monkeypatch,
        tool_results,
    )
    tool_results_map["faq_lookup"] = lambda: {"grounded": False, "fallback": "no faq match"}
    tool_results_map["suggest_products"] = lambda: {
        "spoken_summary": "I found picks from your profile.",
        "picks": [],
    }

    def fake_resolve(number, ctx, client=None):
        ctx["recognition_resolved"] = True
        ctx["known"] = True
        ctx["session_token"] = "phone-backed-token"
        ctx["profile_summary"] = {"top_categories": [("edible", 0.8), ("flower", 0.2)]}
        ctx["caller_phone_hash"] = "k" * 64
        return ctx

    monkeypatch.setattr("voice.tools.suggest.recognition.resolve_caller", fake_resolve)

    out = chat.answer_text_chat(
        {
            "message": "give me something good",
            "store": "yakima",
            "customer_phone": "+15097770011",
        }
    )

    suggest_call = tool_results[-1]
    assert suggest_call[1]["category"] == "edible"
    assert out["safe_next_action"] == "ask_staff"
