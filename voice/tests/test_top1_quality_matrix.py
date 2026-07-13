from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from voice import chat


def _chat_stub(monkeypatch, responses, *, known_caller=False):
    tool_calls = []

    def _dispatch(tool, args, ctx):
        tool_calls.append((tool, dict(args), dict(ctx)))
        if tool == "faq_lookup":
            return responses["faq_lookup"].pop(0)
        return responses["suggest_products"].pop(0)

    def _resolve_caller(number, ctx, client=None):
        ctx["caller_phone_hash"] = "x" * 64
        ctx["recognition_resolved"] = True
        ctx["known"] = bool(known_caller)
        if known_caller and number:
            ctx["session_token"] = "s-known"
            ctx["_caller_phone"] = number
            ctx["profile_summary"] = {"has_history": True, "top_categories": ["flower"], "price_tier": "mid"}
        else:
            ctx["session_token"] = None
            ctx["_caller_phone"] = None
            ctx["profile_summary"] = {"has_history": False, "top_categories": [], "price_tier": ""}
        return ctx

    monkeypatch.setattr(chat, "dispatch", _dispatch)
    monkeypatch.setattr("voice.tools.suggest.recognition.resolve_caller", _resolve_caller)
    return tool_calls


def _load_matrix():
    path = Path(__file__).with_name("data").joinpath("top1_quality_matrix.json")
    return json.loads(path.read_text(encoding="utf-8"))


def _policy_keyword_in(message: str) -> bool:
    return any(
        key in (message or "").lower()
        for key in ("return", "refund", "policy", "age", "wac", "legal", "compliance", "id", "identification")
    )


def _requires_sources(message: str) -> bool:
    return bool(_policy_keyword_in(message))


def _test_case_id(scenario):
    return scenario.get("label", "scenario")


@pytest.mark.parametrize("scenario", _load_matrix(), ids=_test_case_id)
def test_top1_quality_matrix(monkeypatch, scenario):
    faq = scenario["faq"]
    suggest = scenario.get("suggest", {"spoken_summary": "No picks.", "picks": []})
    tool_calls = _chat_stub(
        monkeypatch,
        {"faq_lookup": [faq], "suggest_products": [suggest]},
        known_caller=bool(scenario.get("known_caller")),
    )

    payload = {
        "message": scenario["message"],
        "store": scenario.get("store", "yakima"),
        "slots": scenario.get("slots", {}),
        "session_token": scenario.get("session_token", "s-matrix"),
        "customer_phone": scenario.get("customer_phone"),
        "exclude_skus": scenario.get("exclude_skus"),
    }

    if "expected" in scenario and scenario["expected"].get("error") == "message required":
        out = chat.answer_text_chat(payload)
        assert out.get("error") == "message required"
        return

    out = chat.answer_text_chat(payload)
    expected = scenario["expected"]

    assert out["grounded"] == expected.get("grounded", out["grounded"])
    assert out["safe_next_action"] == expected["safe_next_action"]
    assert out["escalation_required"] == expected.get("escalation_required", out["escalation_required"])

    if expected.get("has_sources"):
        assert out["sources"]
    elif expected.get("has_sources") is False:
        assert not out["sources"]

    if expected.get("safe_contains"):
        assert expected["safe_contains"] in (out["safe_suggested_next_action"] or "").lower()

    if "expect_category" in scenario:
        suggest_call = next((call for call in tool_calls if call[0] == "suggest_products"), None)
        assert suggest_call is not None
        assert suggest_call[1]["category"] == scenario["expect_category"]

    if "expect_suggest_args" in scenario:
        suggest_call = next((call for call in tool_calls if call[0] == "suggest_products"), None)
        assert suggest_call is not None
        for key, value in scenario["expect_suggest_args"].items():
            assert suggest_call[1].get(key) == value

    if out["safe_next_action"] == "show_products":
        picks = out["tool_results"][-1]["result"]["picks"]
        assert picks
        assert len(picks) <= 3
        for pick in picks:
            assert pick.get("why_this")
            assert "price" not in pick
            assert "price_was" not in pick
            assert "margin" not in pick
            assert "cost" not in pick
        assert out.get("safe_suggested_next_action") == "I can show those product options next."


def _compute_scorebook(monkeypatch):
    matrix = _load_matrix()
    escalations = 0
    escalation_empathy = 0
    policy_safe_grounding = 0
    suggestion_paths = 0
    seen_known = False
    seen_anon = False
    for scenario in matrix:
        faq = scenario["faq"]
        suggest = scenario.get("suggest", {"spoken_summary": "No picks.", "picks": []})
        tool_calls = _chat_stub(
            monkeypatch,
            {"faq_lookup": [faq], "suggest_products": [suggest]},
            known_caller=bool(scenario.get("known_caller")),
        )

        payload = {
            "message": scenario["message"],
            "store": scenario.get("store", "yakima"),
            "slots": scenario.get("slots", {}),
            "session_token": scenario.get("session_token", "s-matrix"),
            "customer_phone": scenario.get("customer_phone"),
            "exclude_skus": scenario.get("exclude_skus"),
        }

        if scenario.get("expected", {}).get("error") == "message required":
            continue

        out = chat.answer_text_chat(payload)
        faq_row = faq

        if out["safe_next_action"] == "escalate":
            escalations += 1
            if "sorry" in out["answer"].lower():
                escalation_empathy += 1
            if not out["escalation_required"]:
                raise AssertionError("escalate action missing escalation_required=True")
            if not out["escalation_flag"]:
                raise AssertionError("escalate action missing escalation_flag=True")
            if "sorry" not in out["answer"].lower():
                raise AssertionError("escalation response missing empathy")
            if "staff" not in (out["safe_suggested_next_action"] or "").lower():
                raise AssertionError("escalation next action missing staff handoff text")

        if _requires_sources(scenario["message"]) and faq_row.get("grounded") and not faq_row.get("sources"):
            policy_safe_grounding += 1
            if out["grounded"] is not False:
                raise AssertionError("policy path is grounded without valid sources")
            if out["safe_next_action"] not in {"ask_staff", "escalate"}:
                raise AssertionError("policy without sources did not escalate or ask_staff")

        if out["safe_next_action"] == "show_products":
            suggestion_paths += 1
            tool_call = next((call for call in tool_calls if call[0] == "suggest_products"), None)
            if tool_call is None:
                raise AssertionError("expected suggest_products call for product output path")
            if tool_call[2].get("_caller_phone"):
                seen_known = True
            else:
                seen_anon = True

    return {
        "total_scenarios": len(matrix),
        "evaluated": matrix,
        "escalations": escalations,
        "escalation_empathy": escalation_empathy,
        "policy_safe_grounding": policy_safe_grounding,
        "suggestion_paths": suggestion_paths,
        "seen_known": seen_known,
        "seen_anon": seen_anon,
    }


def test_top1_matrix_scorebook(monkeypatch):
    """Compute the quality invariants we keep to claim top-1% progress."""
    board = _compute_scorebook(monkeypatch)
    escalations = board["escalations"]
    escalation_empathy = board["escalation_empathy"]
    policy_safe_grounding = board["policy_safe_grounding"]
    suggestion_paths = board["suggestion_paths"]
    seen_known = board["seen_known"]
    seen_anon = board["seen_anon"]

    assert escalations >= 4
    assert escalation_empathy == escalations
    assert policy_safe_grounding >= 5
    assert suggestion_paths >= 12
    assert seen_known is True
    assert seen_anon is True

    scorecard_path = os.environ.get("TOP1_SCORECARD_PATH")
    if scorecard_path:
        Path(scorecard_path).parent.mkdir(parents=True, exist_ok=True)
        with open(scorecard_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                "escalations": escalations,
                "escalation_empathy": escalation_empathy,
                "policy_safe_grounding": policy_safe_grounding,
                "suggestion_paths": suggestion_paths,
                "seen_known": seen_known,
                    "seen_anon": seen_anon,
                },
                fh,
                indent=2,
                sort_keys=True,
            )


def test_top1_current_state_gates():
    matrix = _load_matrix()
    conflict = [
        row
        for row in matrix
        if row["expected"].get("safe_next_action") == "escalate"
        and row["expected"].get("error") != "message required"
    ]
    suggest = [row for row in matrix if row["expected"].get("safe_next_action") == "show_products"]
    policy_without_source = [
        row
        for row in matrix
        if row["expected"].get("error") != "message required"
        and _requires_sources(row["message"])
        and row["faq"].get("grounded")
        and not row["faq"].get("sources")
    ]

    assert len(conflict) >= 4
    assert len(suggest) >= 12
    assert len(policy_without_source) >= 2
