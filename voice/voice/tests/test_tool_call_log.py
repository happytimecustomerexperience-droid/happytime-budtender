"""P6: tool calls are logged with args + results, and the full conversation can be fetched from
Vapi (GET /call/{id}) and persisted. Offline — Vapi is mocked.
"""

from __future__ import annotations

import pytest

from voice import callfetch


# ── parse_tool_calls: Vapi artifact.messages → {tool_call_id, name, args, result} ──
def test_parse_tool_calls_matches_results_to_invocations():
    messages = [
        {"role": "bot", "message": "let me check"},
        {
            "role": "tool_calls",
            "toolCalls": [
                {"id": "tc_1", "type": "function",
                 "function": {"name": "faq_lookup", "arguments": '{"query": "deals"}'}},
            ],
        },
        {"role": "tool_call_result", "toolCallId": "tc_1", "name": "faq_lookup",
         "result": {"answer": "30% off flower", "grounded": True}},
    ]
    rows = callfetch.parse_tool_calls(messages)
    assert len(rows) == 1
    assert rows[0]["tool_call_id"] == "tc_1"
    assert rows[0]["name"] == "faq_lookup"
    assert rows[0]["args"] == {"query": "deals"}  # JSON-string arguments coerced to dict
    assert rows[0]["result"]["answer"] == "30% off flower"


def test_parse_tool_calls_handles_missing_result():
    messages = [{"role": "tool_calls",
                 "toolCalls": [{"id": "x", "function": {"name": "suggest_products", "arguments": {}}}]}]
    rows = callfetch.parse_tool_calls(messages)
    assert rows[0]["result"] is None  # invocation with no matching result row


# ── webhook logging: handle_tool_calls persists args + result (redacted) ──────────
@pytest.mark.django_db
def test_handle_tool_calls_logs_each_invocation(monkeypatch):
    from voice import webhooks
    from voice.models import VoiceToolCall

    # Stub the tool dispatch so no real budtender/KB call happens.
    monkeypatch.setattr(webhooks, "dispatch_tool", lambda name, args, ctx: {"ok": True, "echo": args})

    message = {
        "call": {"id": "call_123", "customer": {"number": "+15095551212"}},
        "toolCalls": [
            {"id": "tc_a", "function": {"name": "faq_lookup",
                                        "arguments": {"query": "hours", "store": "yakima"}}},
        ],
    }
    webhooks.handle_tool_calls(message)

    row = VoiceToolCall.objects.get(call_id="call_123", name="faq_lookup")
    assert row.tool_call_id == "tc_a"
    assert row.args["query"] == "hours"
    assert row.result["ok"] is True
    assert row.source == "webhook"


@pytest.mark.django_db
def test_tool_call_args_pii_is_masked():
    """A phone number a caller spoke into an arg is masked before storing (PII discipline)."""
    from voice import guardrails

    masked = guardrails.redact_pii({"caller_name": "Sam at 509-555-1212 please"})
    assert "509-555-1212" not in masked["caller_name"]
    assert "[redacted]" in masked["caller_name"]


# ── fetch_full_conversation: GET /call/{id} → persist transcript + tool calls ──────
@pytest.mark.django_db
def test_fetch_full_conversation_persists(monkeypatch):
    from core.services import vapi
    from voice.models import VoiceCall, VoiceToolCall

    fake_call = {
        "assistantId": "asst_1",
        "analysis": {"summary": "Caller asked about June deals."},
        "artifact": {
            "transcript": "User: what are the deals\nBot: 30% off flower",
            "messages": [
                {"role": "tool_calls",
                 "toolCalls": [{"id": "tc_9", "function": {"name": "faq_lookup",
                                                           "arguments": {"query": "deals"}}}]},
                {"role": "tool_call_result", "toolCallId": "tc_9", "name": "faq_lookup",
                 "result": {"answer": "30% off flower"}},
            ],
        },
    }
    monkeypatch.setattr(vapi, "get_call", lambda cid: fake_call)

    out = callfetch.fetch_full_conversation("call_xyz")
    assert "30% off flower" in out["transcript"]
    assert len(out["tool_calls"]) == 1

    vc = VoiceCall.objects.get(call_id="call_xyz")
    assert "30% off flower" in vc.transcript
    assert vc.ai_summary == "Caller asked about June deals."
    assert VoiceToolCall.objects.filter(call_id="call_xyz", source="vapi_fetch").count() == 1
