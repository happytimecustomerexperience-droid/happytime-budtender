"""tests/test_persona_api.py — GET /api/voice/persona (the website chat's Vertex-fallback
persona feed; root project's budtender/gemini_chat.py::fetch_persona contract).

Asserts: 401 without the Bearer token; 200 with the AgentPrompt-shaped body (tone sentence +
IMMUTABLE runtime-safety block present); 404 when the "written" row is missing; the greeting
matches entry_router.first_message; and build_assistant_payload("entry_router")["firstMessage"]
tracks the row.
"""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_persona_requires_bearer_token(client, settings):
    settings.HHT_BACKEND_TOKEN = "test-token"
    resp = client.get("/api/voice/persona")
    assert resp.status_code == 401
    assert resp.json()["ok"] is False


@pytest.mark.django_db
def test_persona_404_when_written_row_missing(client, settings):
    settings.HHT_BACKEND_TOKEN = "test-token"
    resp = client.get("/api/voice/persona", **{"HTTP_AUTHORIZATION": "Bearer test-token"})
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


@pytest.mark.django_db
def test_persona_returns_expected_shape(client, settings):
    from kb.models import AgentPrompt

    settings.HHT_BACKEND_TOKEN = "test-token"
    AgentPrompt.objects.create(
        role="written",
        body="You are the warm, friendly voice of Happy Time Weed, a family-owned Washington cannabis shop.",
        is_active=True,
    )
    AgentPrompt.objects.create(
        role="entry_router",
        body="Entry router body.",
        first_message="Welcome to Happy Time! What can I do for you today?",
        is_active=True,
    )

    resp = client.get("/api/voice/persona", **{"HTTP_AUTHORIZATION": "Bearer test-token"})

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"ok", "written_system_instruction", "greeting", "updated_at"}
    assert body["ok"] is True
    assert "warm, friendly voice of Happy Time Weed" in body["written_system_instruction"]
    assert "IMMUTABLE RUNTIME SAFETY" in body["written_system_instruction"]
    assert body["greeting"] == "Welcome to Happy Time! What can I do for you today?"
    assert body["updated_at"]  # iso8601 string, non-empty
    assert body["written_system_instruction"][:200]


@pytest.mark.django_db
def test_entry_greeting_matches_first_message_row():
    from kb.models import AgentPrompt
    from voice.provision import entry_greeting

    AgentPrompt.objects.create(
        role="entry_router", body="x", first_message="Hi there!", is_active=True
    )
    assert entry_greeting() == "Hi there!"


@pytest.mark.django_db
def test_entry_greeting_empty_when_no_row():
    from voice.provision import entry_greeting

    assert entry_greeting() == ""


@pytest.mark.django_db
def test_build_assistant_payload_uses_row_first_message():
    from kb.models import AgentPrompt
    from voice.provision import build_assistant_payload

    AgentPrompt.objects.create(
        role="entry_router", body="x", first_message="Custom greeting.", is_active=True
    )
    payload, warnings = build_assistant_payload("entry_router")
    assert payload["firstMessage"] == "Custom greeting."
    assert not any("first_message" in w for w in warnings)


@pytest.mark.django_db
def test_build_assistant_payload_warns_and_omits_when_first_message_blank():
    from kb.models import AgentPrompt
    from voice.provision import build_assistant_payload

    AgentPrompt.objects.create(role="entry_router", body="x", first_message="", is_active=True)
    payload, warnings = build_assistant_payload("entry_router")
    assert "firstMessage" not in payload
    assert "entry_router has no first_message" in warnings
