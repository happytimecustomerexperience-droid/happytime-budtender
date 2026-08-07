"""Agent test console (playground) — the staff surface for texting/talking to the live brain.

The console MUST drive the same ``voice.chat.answer_text_chat`` the Vapi phone agent and the
website chat use (never a fork of it), and every console turn MUST land in the existing call
models so a test session is auditable from the normal dashboard call views. Offline, SQLite.
"""

from __future__ import annotations

import json

import pytest
from django.urls import reverse


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="owner", password="x", is_staff=True, is_superuser=True
    )
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_console_page_renders_both_input_modes(staff_client):
    """One page, three ways in: type, browser mic, real Vapi call."""
    resp = staff_client.get(reverse("dash-playground"))
    assert resp.status_code == 200
    body = resp.content.decode()
    assert "playground-send" in body or reverse("dash-playground-send") in body
    assert "webkitSpeechRecognition" in body, "browser mic path missing"
    assert "vapi" in body.lower(), "real Vapi call path missing"


@pytest.mark.django_db
def test_send_returns_the_full_diagnostic_envelope(staff_client):
    """The console is useless without the trace — answer AND why the agent said it."""
    resp = staff_client.post(
        reverse("dash-playground-send"),
        data=json.dumps({"message": "what are your hours", "store": "yakima"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    data = resp.json()
    for key in ("answer", "intent", "grounded", "sources", "tool_results", "call_id", "latency_ms"):
        assert key in data, f"missing diagnostic key: {key}"
    assert data["tool_results"], "no tool trace returned"
    assert data["tool_results"][0]["tool"] == "faq_lookup"


@pytest.mark.django_db
def test_turn_is_persisted_through_the_existing_call_models(staff_client):
    """A console session must be readable from the normal dashboard call log — no new storage."""
    from voice.models import VoiceCall, VoiceToolCall, VoiceTurn

    resp = staff_client.post(
        reverse("dash-playground-send"),
        data=json.dumps({"message": "what are your hours", "store": "yakima"}),
        content_type="application/json",
    )
    call_id = resp.json()["call_id"]
    assert call_id.startswith("pg-")

    call = VoiceCall.objects.get(call_id=call_id)
    assert call.store == "yakima"
    roles = list(call.turns.order_by("seq").values_list("role", flat=True))
    assert roles == ["user", "assistant"]
    assert VoiceToolCall.objects.filter(call_id=call_id, source="playground").exists()
    assert VoiceTurn.objects.filter(call=call, role="assistant").first().latency_ms is not None


@pytest.mark.django_db
def test_same_session_id_appends_turns_rather_than_starting_over(staff_client):
    from voice.models import VoiceCall

    first = staff_client.post(
        reverse("dash-playground-send"),
        data=json.dumps({"message": "what are your hours", "store": "yakima"}),
        content_type="application/json",
    ).json()
    staff_client.post(
        reverse("dash-playground-send"),
        data=json.dumps(
            {"message": "what is your return policy", "store": "yakima", "call_id": first["call_id"]}
        ),
        content_type="application/json",
    )
    call = VoiceCall.objects.get(call_id=first["call_id"])
    assert call.turns.count() == 4, "second turn started a new session instead of appending"


@pytest.mark.django_db
def test_empty_message_is_rejected(staff_client):
    resp = staff_client.post(
        reverse("dash-playground-send"),
        data=json.dumps({"message": "   "}),
        content_type="application/json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_vapi_call_button_is_gated_on_a_configured_public_key(staff_client, settings):
    """Without a public key there is no web-call credential — say so instead of a dead button."""
    settings.VAPI_PUBLIC_KEY = ""
    resp = staff_client.get(reverse("dash-playground"))
    assert resp.context["vapi_ready"] is False

    settings.VAPI_PUBLIC_KEY = "pk_test"
    from kb.models import AgentPrompt

    AgentPrompt.objects.create(role="faq", is_active=True, vapi_assistant_id="asst_123")
    resp = staff_client.get(reverse("dash-playground"))
    assert resp.context["vapi_ready"] is True
    assert resp.context["vapi_assistant_id"] == "asst_123"


@pytest.mark.django_db
def test_console_never_leaks_cost_or_margin(staff_client):
    """Leak-guard holds on the console exactly as it does on the phone."""
    resp = staff_client.post(
        reverse("dash-playground-send"),
        data=json.dumps({"message": "what are your hours", "store": "yakima"}),
        content_type="application/json",
    )
    blob = json.dumps(resp.json()).lower()
    assert "margin" not in blob
    assert '"cost"' not in blob
