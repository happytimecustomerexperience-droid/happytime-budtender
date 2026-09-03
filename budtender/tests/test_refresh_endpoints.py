import json
import os
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from budtender import gemini_chat
from core import store_facts

TOKEN = "test-token"

ENV = {
    "HHT_VOICE_BASE_URL": "http://voice.internal:8000",
    "HHT_BACKEND_TOKEN": TOKEN,
}

PERSONA = {
    "ok": True,
    "written_system_instruction": "You are Happy Time's warm, no-nonsense budtender.",
    "greeting": "Hey!",
    "updated_at": "2026-09-02T00:00:00Z",
}

STORE_FACTS = {
    "ok": True,
    "stores": {"yakima": {"hours": "8-10", "address": "1 Main St, Yakima, WA 98901", "phone": "555"}},
    "global": {"payment": "cash", "age": "21+", "pickup": "in-store"},
    "updated_at": "2026-09-02T00:00:00Z",
}


class Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"

    def json(self):
        return self._payload


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class RefreshEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        gemini_chat.invalidate_persona()
        gemini_chat._persona_warned_at = None
        store_facts.invalidate()
        self.addCleanup(gemini_chat.invalidate_persona)
        self.addCleanup(store_facts.invalidate)

    def _auth(self):
        return {"HTTP_AUTHORIZATION": f"Bearer {TOKEN}"}

    # -- persona/refresh --------------------------------------------------

    def test_persona_refresh_requires_token(self):
        r = self.client.post("/api/v1/persona/refresh")
        self.assertIn(r.status_code, (401, 403))

    def test_persona_refresh_invalidates_and_refetches(self):
        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get", return_value=Resp(200, PERSONA)
        ) as fake_get:
            r = self.client.post("/api/v1/persona/refresh", **self._auth())

        self.assertEqual(r.status_code, 200)
        body = json.loads(r.content)
        self.assertTrue(body["ok"])
        self.assertIn("updated_at", body)
        self.assertEqual(fake_get.call_count, 1)

    def test_persona_refresh_reports_unreachable_without_5xx(self):
        with patch.dict(os.environ, ENV), patch(
            "budtender.gemini_chat.requests.get",
            side_effect=gemini_chat.requests.RequestException("boom"),
        ):
            r = self.client.post("/api/v1/persona/refresh", **self._auth())

        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True, "refreshed": False})

    # -- store-facts/refresh ------------------------------------------------

    def test_store_facts_refresh_requires_token(self):
        r = self.client.post("/api/v1/store-facts/refresh")
        self.assertIn(r.status_code, (401, 403))

    def test_store_facts_refresh_invalidates_and_refetches(self):
        with patch.dict(os.environ, ENV), patch(
            "core.store_facts.requests.get", return_value=Resp(200, STORE_FACTS)
        ) as fake_get:
            r = self.client.post("/api/v1/store-facts/refresh", **self._auth())

        self.assertEqual(r.status_code, 200)
        body = json.loads(r.content)
        self.assertTrue(body["ok"])
        self.assertEqual(body["stores"], ["yakima"])
        self.assertIn("updated_at", body)
        self.assertEqual(fake_get.call_count, 1)

    def test_store_facts_refresh_reports_unreachable_without_5xx(self):
        with patch.dict(os.environ, ENV), patch(
            "core.store_facts.requests.get",
            side_effect=store_facts.requests.RequestException("boom"),
        ):
            r = self.client.post("/api/v1/store-facts/refresh", **self._auth())

        self.assertEqual(r.status_code, 200)
        self.assertEqual(json.loads(r.content), {"ok": True, "refreshed": False})
