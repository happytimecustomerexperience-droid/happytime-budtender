"""P6: the dashboard's per-row model + voice config actually reaches the Vapi payload.

The pre-P6 bug: ``build_assistant_payload`` hardcoded the model/voice constants and ignored the
saved ``AgentPrompt`` fields, so editing model/voice in the dashboard had zero live effect. These
checks pin the fix — the row drives provider/model/temperature/maxTokens + the ElevenLabs voice
switch — and the constant fallback still works on a bare tree (no row).
"""

from __future__ import annotations

import pytest

from voice import constants as C
from voice import provision


@pytest.mark.django_db
def test_model_and_voice_read_from_agentprompt_row():
    """An AgentPrompt with Gemini + ElevenLabs config drives the assistant payload (not constants)."""
    from kb.models import AgentPrompt

    AgentPrompt.objects.create(
        role="faq",
        body="Be helpful.",
        model_provider="google",
        vapi_model="gemini-2.5-flash",
        temperature=0.42,
        max_output_tokens=321,
        voice_provider="11labs",
        voice_id="el_voice_123",
        voice_settings={"stability": 0.5, "similarityBoost": 0.75, "model": "eleven_flash_v2_5"},
        tool_names=[],
        is_active=True,
    )
    payload, _warnings = provision.build_assistant_payload("faq", name="faq")

    # Model block comes from the row.
    assert payload["model"]["provider"] == "google"
    assert payload["model"]["model"] == "gemini-2.5-flash"
    assert payload["model"]["temperature"] == 0.42
    assert payload["model"]["maxTokens"] == 321

    # Voice block switched to ElevenLabs with the row's voiceId + spread knobs.
    voice = payload["voice"]
    assert voice["provider"] == "11labs"
    assert voice["voiceId"] == "el_voice_123"
    assert voice["model"] == "eleven_flash_v2_5"
    assert voice["stability"] == 0.5
    assert voice["similarityBoost"] == 0.75


@pytest.mark.django_db
def test_falls_back_to_constants_with_no_row():
    """A bare tree (no AgentPrompt) still provisions: constants are the fallback (Cartesia + openai)."""
    payload, warnings = provision.build_assistant_payload("faq", name="faq")
    assert payload["model"]["provider"] == C.ASSISTANT_PROVIDER
    assert payload["model"]["model"] == C.ASSISTANT_MODEL
    assert payload["voice"]["provider"] == "cartesia"
    assert payload["voice"]["voiceId"] == C.CARTESIA_VOICE["voiceId"]
    assert any("no AgentPrompt" in w for w in warnings)


@pytest.mark.django_db
def test_partial_row_uses_constant_and_entry_router_token_fallback():
    """A row that sets provider/model but leaves temperature/max_output_tokens null falls back to
    the constants, and entry_router keeps its tight 200-token default."""
    from kb.models import AgentPrompt

    AgentPrompt.objects.create(
        role="entry_router", body="route", model_provider="google", vapi_model="gemini-2.5-flash",
        temperature=None, max_output_tokens=None, is_active=True,
    )
    payload, _ = provision.build_assistant_payload("entry_router", name="entry_router")
    assert payload["model"]["temperature"] == C.ASSISTANT_TEMPERATURE  # null → constant
    assert payload["model"]["maxTokens"] == 200  # entry_router default kept


@pytest.mark.django_db
def test_cartesia_per_row_voice_override():
    """A Cartesia row with a custom voice_id + voice_settings overrides the constant voice block."""
    from kb.models import AgentPrompt

    AgentPrompt.objects.create(
        role="faq", body="x", voice_provider="cartesia", voice_id="custom-cartesia-id",
        voice_settings={"speed": "fast"}, is_active=True,
    )
    payload, _ = provision.build_assistant_payload("faq", name="faq")
    assert payload["voice"]["provider"] == "cartesia"
    assert payload["voice"]["voiceId"] == "custom-cartesia-id"
    assert payload["voice"]["speed"] == "fast"  # voice_settings spread through
