import os
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from budtender import gemini_chat

ENV = {
    "HHT_VOICE_BASE_URL": "http://voice.internal:8000",
    "HHT_BACKEND_TOKEN": "secret-token",
}

PERSONA = {
    "ok": True,
    "written_system_instruction": "You are Happy Time's warm, no-nonsense budtender.",
    "greeting": "Hey! What are you looking for today?",
    "updated_at": "2026-09-01T00:00:00Z",
}


class Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"

    def json(self):
        return self._payload


class FetchPersonaTests(SimpleTestCase):
    def setUp(self):
        gemini_chat._persona_cache["value"] = None
        gemini_chat._persona_cache["fetched_at"] = None
        gemini_chat._persona_warned_at = None

    def test_fresh_fetch_populates_cache_and_feeds_system_instruction(self):
        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get", return_value=Resp(200, PERSONA)
        ) as fake_get, self.assertLogs("budtender.gemini_chat", level="INFO") as logs:
            text = gemini_chat.system_instruction()

        self.assertEqual(text, PERSONA["written_system_instruction"])
        self.assertEqual(fake_get.call_count, 1)
        self.assertTrue(any("using shared AgentPrompt" in line for line in logs.output))
        self.assertEqual(gemini_chat.greeting(), PERSONA["greeting"])

    def test_second_call_within_ttl_makes_no_http_call(self):
        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get", return_value=Resp(200, PERSONA)
        ) as fake_get:
            gemini_chat.fetch_persona()
            gemini_chat.fetch_persona()

        self.assertEqual(fake_get.call_count, 1)

    def test_endpoint_500_after_good_fetch_uses_stale_cache(self):
        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get", return_value=Resp(200, PERSONA)
        ):
            gemini_chat.fetch_persona()

        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get", return_value=Resp(500, {})
        ):
            persona = gemini_chat.fetch_persona(force=True)

        self.assertEqual(persona["written_system_instruction"], PERSONA["written_system_instruction"])

    def test_endpoint_down_with_empty_cache_falls_back_to_safety_only_and_warns(self):
        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get", side_effect=gemini_chat.requests.RequestException("boom")
        ), self.assertLogs("budtender.gemini_chat", level="WARNING") as logs:
            text = gemini_chat.system_instruction()

        self.assertEqual(text, gemini_chat._SAFETY_ONLY_INSTRUCTION)
        self.assertTrue(any("voice service unreachable" in line for line in logs.output))
        self.assertEqual(gemini_chat.greeting(), "")

    def test_fallback_reply_passes_system_instruction_into_generate_content_config(self):
        messages = [SimpleNamespace(role="user", content="hello")]
        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get", return_value=Resp(200, PERSONA)
        ), patch(
            "budtender.gemini_chat._voice_chat", return_value=None
        ), patch(
            "budtender.gemini_chat._voice_grounding", return_value=None
        ), patch(
            "budtender.gemini_chat._client"
        ) as fake_client:
            fake_client.return_value.models.generate_content.return_value = SimpleNamespace(
                text="Vertex answered this one."
            )
            gemini_chat.generate_chat_reply_with_source(messages, store="yakima")

        _, kwargs = fake_client.return_value.models.generate_content.call_args
        self.assertEqual(
            kwargs["config"].system_instruction, PERSONA["written_system_instruction"]
        )
