"""voice/ P0 tests — the webhook contract + HMAC gate + tool registry + grounded FAQ.

All external calls are mocked: the FAQ tool runs the KB keyword fallback (semantic OFF via the
conftest autouse fixture → no Gemini), and the summarizer never fires on the tool path. The
suite passes with NO live API keys (03-CONVENTIONS.md §5).

The four required cases (task spec):
  (a) a mock ``assistant-request`` → 200 + the assistant config (hydrated variables).
  (b) a mock ``faq_lookup`` ``tool-calls`` → a grounded answer from KB.
  (c) a bad / missing HMAC signature → rejected 401/403 (fail-closed).
  (d) BOTH ``toolCalls`` and ``toolCallList`` shapes parse (R-2).
Plus: the central leak-scrub wall and the eocr durable write.
"""

from __future__ import annotations

import json

import pytest

from voice import guardrails, signing
from voice.webhooks import _extract_tool_calls

WEBHOOK_URL = "/api/voice/vapi"
SECRET = "test-webhook-secret-0123456789"


@pytest.fixture(autouse=True)
def _webhook_secret(settings):
    """Configure the webhook secret so the HMAC gate is live for every test."""
    settings.VAPI_WEBHOOK_SECRET = SECRET
    settings.VAPI_SIGNATURE_HEADER = "X-Vapi-Signature"
    settings.VAPI_SECRET_HEADER = "X-Vapi-Secret"
    settings.HHT_DEFAULT_STORE = "yakima"


def _post_signed(client, payload: dict):
    """POST a JSON payload with a valid Mode-A HMAC signature header."""
    raw = json.dumps(payload).encode()
    sig = signing.compute_signature(raw, SECRET)
    return client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SIGNATURE": sig},
    )


def _post_secret(client, payload: dict):
    """POST with the Mode-B shared-secret header instead of an HMAC signature."""
    raw = json.dumps(payload).encode()
    return client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SECRET": SECRET},
    )


# ── (c) HMAC fail-closed ───────────────────────────────────────────────────────


@pytest.mark.django_db
def test_missing_signature_rejected(client):
    """No signature/secret header → 401, before any handler runs (fail-closed)."""
    raw = json.dumps({"message": {"type": "status-update"}}).encode()
    resp = client.post(WEBHOOK_URL, data=raw, content_type="application/json")
    assert resp.status_code == 401


@pytest.mark.django_db
def test_bad_signature_rejected(client):
    """A wrong HMAC signature → 401 (constant-time compare, reject-by-default)."""
    raw = json.dumps({"message": {"type": "status-update"}}).encode()
    resp = client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SIGNATURE": "deadbeef" * 8},
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_bad_shared_secret_rejected(client):
    """A wrong Mode-B shared secret → 401."""
    raw = json.dumps({"message": {"type": "status-update"}}).encode()
    resp = client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SECRET": "not-the-secret"},
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_unconfigured_secret_fails_closed(client, settings):
    """An unconfigured webhook secret rejects (never opens the gate) even with a header present."""
    settings.VAPI_WEBHOOK_SECRET = ""
    raw = json.dumps({"message": {"type": "status-update"}}).encode()
    resp = client.post(
        WEBHOOK_URL,
        data=raw,
        content_type="application/json",
        **{"HTTP_X_VAPI_SECRET": "anything"},
    )
    assert resp.status_code == 401


@pytest.mark.django_db
def test_valid_signature_accepted(client):
    """A correct Mode-A signature → 200 (the happy path the rest of the suite relies on)."""
    resp = _post_signed(client, {"message": {"type": "status-update", "call": {"id": "c1"}}})
    assert resp.status_code == 200


@pytest.mark.django_db
def test_valid_shared_secret_accepted(client):
    """A correct Mode-B shared secret → 200."""
    resp = _post_secret(client, {"message": {"type": "status-update", "call": {"id": "c2"}}})
    assert resp.status_code == 200


# ── (a) assistant-request returns the assistant config ─────────────────────────


@pytest.mark.django_db
def test_assistant_request_returns_config(client):
    """A mock assistant-request → 200 + hydrated variableValues (no literal {{store_name}})."""
    from kb.models import AgentPrompt, StoreFact

    AgentPrompt.objects.create(
        role="faq", body="persona", vapi_assistant_id="asst_test_123", is_active=True
    )
    StoreFact.objects.create(
        store="yakima", kind="hours", label="Yakima hours", value="9 AM–11 PM daily", confirmed=True
    )

    resp = _post_signed(
        client,
        {
            "message": {
                "type": "assistant-request",
                "call": {"id": "c3", "customer": {"number": "+15095551212"}},
            }
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["assistantId"] == "asst_test_123"
    variables = body["assistantOverrides"]["variableValues"]
    assert variables["store_name"] == "Happy Time Yakima"
    assert variables["store_hours"] == "9 AM–11 PM daily"
    assert "{{" not in json.dumps(variables)  # fully hydrated


# ── (b) faq_lookup tool-call returns a grounded answer ─────────────────────────


@pytest.mark.django_db
def test_tool_call_faq_lookup_grounded(client):
    """A mock faq_lookup tool-call → the Vapi tool-result envelope with a grounded KB answer.

    Semantic search is OFF in tests (conftest), so grounding runs the deterministic keyword
    fallback over real KB rows — exactly the degrade-safe path that fires when Gemini is down.
    The query tokens overlap the seeded FAQEntry question/paraphrases."""
    from kb.models import FAQEntry

    FAQEntry.objects.create(
        key="hours-yakima",
        question="What time do you close in Yakima?",
        answer="Our Yakima store is open until 11 PM tonight.",
        paraphrases=["closing time", "when do you close"],
        store="yakima",
        topic="hours",
        weight=200,
    )

    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": "c4", "customer": {"number": "+15095551212"}},
            "toolCalls": [
                {
                    "id": "call_abc",
                    "function": {
                        "name": "faq_lookup",
                        "arguments": {"query": "what time do you close", "store": "yakima"},
                    },
                }
            ],
        }
    }
    resp = _post_signed(client, payload)
    assert resp.status_code == 200
    body = resp.json()
    result = body["results"][0]
    assert result["toolCallId"] == "call_abc"
    assert result["result"]["grounded"] is True
    assert "11 PM" in result["result"]["answer"]
    assert result["result"]["sources"][0]["kind"] == "faq"


@pytest.mark.django_db
def test_faq_lookup_grounds_via_embeddings_when_semantic_on(settings, monkeypatch):
    """The embedding (semantic) path also grounds — Gemini ``embed`` MOCKED with a deterministic
    bag-of-words vector (semantically meaningful overlap, offline, no live key). Exercises the
    cosine path in faq.py directly so the floor logic is covered for ``semantic.enabled()``."""
    settings.SEMANTIC_SEARCH_ENABLED = True
    from core.services import gemini as gemini_mod
    from kb.models import FAQEntry
    from voice.tools import faq

    # A hashed bag-of-words embedder into a FIXED-dim space, so query + corpus vectors are
    # always the same length (the per-call vocab-growth mock breaks _cos's length check).
    # Cosine reflects real token overlap (unlike the conftest hash mock), exercising grounding.
    _DIM = 1024

    def _bow_embed(texts, *, task_type="RETRIEVAL_DOCUMENT", **kw):
        import hashlib
        import math

        one = isinstance(texts, str)
        items = [texts] if one else list(texts)
        out = []
        for t in items:
            v = [0.0] * _DIM
            for tok in t.lower().split():
                idx = int(hashlib.sha256(tok.encode()).hexdigest(), 16) % _DIM
                v[idx] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out[0] if one else out

    monkeypatch.setattr(gemini_mod, "embed", _bow_embed)
    monkeypatch.setattr(gemini_mod, "active_embedding_model", lambda: "bow-mock")

    FAQEntry.objects.create(
        key="hours-yakima-emb",
        question="What time do you close in Yakima today",
        answer="Our Yakima store is open until 11 PM tonight.",
        store="yakima",
        topic="hours",
        weight=200,
    )
    out = faq.faq_lookup({"query": "what time do you close in yakima today", "store": "yakima"}, {})
    assert out["grounded"] is True
    assert "11 PM" in out["answer"]


@pytest.mark.django_db
def test_tool_call_no_match_offers_human(client):
    """No KB match → grounded:false + a human-handoff fallback (never an invented number)."""
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": "c5"},
            "toolCalls": [
                {
                    "id": "x",
                    "function": {"name": "faq_lookup", "arguments": {"query": "zzqqxx nonsense"}},
                }
            ],
        }
    }
    resp = _post_signed(client, payload)
    assert resp.status_code == 200
    result = resp.json()["results"][0]["result"]
    assert result["grounded"] is False
    assert result["answer"] is None
    assert result["fallback"]


@pytest.mark.django_db
def test_unknown_tool_is_structured_not_500(client):
    """An unknown tool name → a structured error, never a 500."""
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": "c6"},
            "toolCalls": [{"id": "y", "function": {"name": "no_such_tool", "arguments": {}}}],
        }
    }
    resp = _post_signed(client, payload)
    assert resp.status_code == 200
    result = resp.json()["results"][0]["result"]
    assert result["error"] == "unknown_tool"


# ── (d) BOTH toolCalls and toolCallList shapes parse (R-2) ─────────────────────


def test_extract_tool_calls_both_field_names():
    """R-2: ``toolCalls`` AND ``toolCallList`` both normalize to one internal shape."""
    a = _extract_tool_calls(
        {
            "toolCalls": [
                {"id": "1", "function": {"name": "faq_lookup", "arguments": {"query": "hi"}}}
            ]
        }
    )
    b = _extract_tool_calls(
        {
            "toolCallList": [
                {"id": "2", "function": {"name": "faq_lookup", "arguments": {"query": "hi"}}}
            ]
        }
    )
    assert a == [{"id": "1", "name": "faq_lookup", "arguments": {"query": "hi"}}]
    assert b == [{"id": "2", "name": "faq_lookup", "arguments": {"query": "hi"}}]


def test_extract_tool_calls_arguments_as_json_string():
    """R-2: stringified ``arguments`` (some Vapi versions) are coerced to a dict."""
    out = _extract_tool_calls(
        {
            "toolCalls": [
                {"id": "3", "function": {"name": "faq_lookup", "arguments": '{"query": "x"}'}}
            ]
        }
    )
    assert out == [{"id": "3", "name": "faq_lookup", "arguments": {"query": "x"}}]


@pytest.mark.django_db
def test_tool_call_via_toolcalllist_shape_grounded(client):
    """End-to-end: a tool-call delivered under ``toolCallList`` still answers grounded."""
    from kb.models import FAQEntry

    FAQEntry.objects.create(
        key="payment",
        question="How do I pay — do you take cards?",
        answer="Cash and debit only, and there's an on-site ATM.",
        paraphrases=["payment methods", "do you take cards"],
        topic="payment",
        weight=200,
    )
    payload = {
        "message": {
            "type": "tool-calls",
            "call": {"id": "c7"},
            "toolCallList": [
                {
                    "id": "z",
                    "function": {
                        "name": "faq_lookup",
                        "arguments": {"query": "how do I pay", "store": "yakima"},
                    },
                }
            ],
        }
    }
    resp = _post_signed(client, payload)
    assert resp.status_code == 200
    result = resp.json()["results"][0]["result"]
    assert result["grounded"] is True
    assert "debit" in result["answer"].lower()


# ── Leak-Guard (central scrub wall) ────────────────────────────────────────────


def test_scrub_leak_drops_forbidden_keys():
    cleaned = guardrails.scrub_leak(
        {"name": "Blue Dream", "cost": 4.2, "margin": 0.4, "ok": [1, 2]}
    )
    assert "cost" not in cleaned and "margin" not in cleaned
    assert cleaned == {"name": "Blue Dream", "ok": [1, 2]}


def test_scrub_leak_nukes_forbidden_substring():
    cleaned = guardrails.scrub_leak({"answer": "our margin is 38%"})
    assert cleaned == {"error": "redacted", "reason": "leak_blocked"}


def test_assert_no_leak_raises():
    with pytest.raises(guardrails.LeakError):
        guardrails.assert_no_leak({"cost": 1})


def test_dispatch_applies_scrub_centrally():
    """A handler that leaks is scrubbed by the registry dispatch (no per-tool opt-in)."""
    from voice import tools

    @tools.register("_leaky_test_tool")
    def _leaky(args, ctx):
        return {"answer": "fine", "cost": 9.99}

    out = tools.dispatch("_leaky_test_tool", {}, {})
    assert "cost" not in out
    tools.TOOL_REGISTRY.pop("_leaky_test_tool", None)


# ── eocr durable write ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_end_of_call_report_writes_durable_record(client):
    """eocr → an idempotent VoiceCall row (phone hashed, raw number NEVER stored)."""
    from voice.models import VoiceCall

    payload = {
        "message": {
            "type": "end-of-call-report",
            "call": {
                "id": "call_eocr_1",
                "customer": {"number": "+15095551212"},
                "assistantId": "asst_x",
            },
            "endedReason": "customer-ended-call",
            "durationSeconds": 42,
            "transcript": "Q: what time do you close? A: 11 PM.",
            "messages": [{"role": "user", "message": "what time do you close"}],
        }
    }
    resp = _post_signed(client, payload)
    assert resp.status_code == 200

    vc = VoiceCall.objects.get(call_id="call_eocr_1")
    assert vc.duration_s == 42
    assert vc.outcome == "faq_answered"
    assert vc.caller_phone_hash and len(vc.caller_phone_hash) == 64
    # The raw number is never persisted anywhere on the row.
    assert "+15095551212" not in (vc.transcript + vc.caller_phone_hash)
    assert vc.turns.count() == 1

    # Idempotent re-delivery: same call_id upserts, never duplicates.
    resp2 = _post_signed(client, payload)
    assert resp2.status_code == 200
    assert VoiceCall.objects.filter(call_id="call_eocr_1").count() == 1


@pytest.mark.django_db
def test_unknown_message_type_400(client):
    resp = _post_signed(client, {"message": {"type": "no-such-event"}})
    assert resp.status_code == 400
