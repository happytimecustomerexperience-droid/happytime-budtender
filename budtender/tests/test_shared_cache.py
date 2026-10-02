"""Persona and store-facts caches are shared across gunicorn workers.

They used to be per-process dicts, so a dashboard "refresh" reached one worker and the
other four kept serving the old value for up to a TTL (5-10 min). A second worker is
simulated by reloading the module, which throws away every module-level variable exactly
as a freshly forked worker starts without them; only Django's cache survives.
"""
import importlib
import json
import os
from unittest.mock import MagicMock, patch

from django.test import Client, TestCase, override_settings

from budtender import gemini_chat
from core import store_facts

TOKEN = "test-token"
ENV = {"HHT_VOICE_BASE_URL": "http://voice.internal:8000", "HHT_BACKEND_TOKEN": TOKEN}


class Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"

    def json(self):
        return self._payload


def persona(text):
    return {"ok": True, "written_system_instruction": text, "greeting": "Hey!", "updated_at": "2026-09-02T00:00:00Z"}


def facts(hours):
    return {"ok": True, "stores": {"yakima": {"hours": hours, "phone": "555"}}, "global": {}, "updated_at": "x"}


def new_worker(module):
    """A freshly forked gunicorn worker: module-level state is gone, only the shared cache is left."""
    return importlib.reload(module)


@override_settings(HHT_BACKEND_TOKEN=TOKEN)
class SharedCacheTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.addCleanup(new_worker, gemini_chat)
        self.addCleanup(new_worker, store_facts)

    def _refresh(self, path):
        return self.client.post(path, HTTP_AUTHORIZATION=f"Bearer {TOKEN}")

    # -- persona -------------------------------------------------------------------------------

    def test_a_second_worker_serves_the_persona_without_calling_voice(self):
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.requests.get", return_value=Resp(200, persona("v1"))):
            gemini_chat.fetch_persona()                                   # worker A warms the cache

        new_worker(gemini_chat)                                           # worker B, cold process
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.requests.get") as get_b:
            got = gemini_chat.fetch_persona()

        self.assertEqual(got["written_system_instruction"], "v1")
        get_b.assert_not_called()

    def test_a_persona_refresh_taken_by_one_worker_is_seen_by_the_others(self):
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.requests.get", return_value=Resp(200, persona("v1"))):
            gemini_chat.fetch_persona()

        # The owner edits the persona; the dashboard's refresh nudge lands on ONE worker.
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.requests.get", return_value=Resp(200, persona("v2"))):
            r = self._refresh("/api/v1/persona/refresh")
        self.assertEqual(json.loads(r.content)["ok"], True)

        new_worker(gemini_chat)                                           # a different worker, v1 never seen
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.requests.get") as get_b:
            got = gemini_chat.fetch_persona()
        self.assertEqual(got["written_system_instruction"], "v2")
        get_b.assert_not_called()

    def test_the_persona_failure_back_off_is_shared_too(self):
        boom = gemini_chat.requests.RequestException("down")
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.requests.get", side_effect=boom) as get_a:
            self.assertIsNone(gemini_chat.fetch_persona())
        self.assertEqual(get_a.call_count, 1)

        new_worker(gemini_chat)
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.requests.get", side_effect=boom) as get_b:
            self.assertIsNone(gemini_chat.fetch_persona())
        get_b.assert_not_called()          # worker B does not re-pay the connect timeout inside the window

    def test_a_cache_outage_never_raises_out_of_fetch_persona(self):
        broken = MagicMock()
        broken.get.side_effect = ConnectionError("redis down")
        broken.set.side_effect = ConnectionError("redis down")
        broken.delete.side_effect = ConnectionError("redis down")
        with patch.dict(os.environ, ENV), patch("budtender.gemini_chat.cache", broken), patch(
            "budtender.gemini_chat.requests.get", return_value=Resp(200, persona("v1"))
        ):
            got = gemini_chat.fetch_persona()
            gemini_chat.invalidate_persona()
        self.assertEqual(got["written_system_instruction"], "v1")

    # -- store facts ----------------------------------------------------------------------------

    def test_a_second_worker_serves_store_facts_without_calling_voice(self):
        with patch.dict(os.environ, ENV), patch("core.store_facts.requests.get", return_value=Resp(200, facts("8-10"))):
            store_facts.fetch_store_facts()

        new_worker(store_facts)
        with patch.dict(os.environ, ENV), patch("core.store_facts.requests.get") as get_b:
            got = store_facts.fetch_store_facts()

        self.assertEqual(got["stores"]["yakima"]["hours"], "8-10")
        get_b.assert_not_called()

    def test_a_store_facts_refresh_taken_by_one_worker_is_seen_by_the_others(self):
        with patch.dict(os.environ, ENV), patch("core.store_facts.requests.get", return_value=Resp(200, facts("8-10"))):
            store_facts.fetch_store_facts()

        with patch.dict(os.environ, ENV), patch("core.store_facts.requests.get", return_value=Resp(200, facts("9-11"))):
            r = self._refresh("/api/v1/store-facts/refresh")
        self.assertEqual(json.loads(r.content)["ok"], True)

        new_worker(store_facts)
        with patch.dict(os.environ, ENV), patch("core.store_facts.requests.get") as get_b:
            got = store_facts.fetch_store_facts()
        self.assertEqual(got["stores"]["yakima"]["hours"], "9-11")
        get_b.assert_not_called()

    def test_stale_store_facts_outlive_the_ttl_while_voice_is_down(self):
        with patch.dict(os.environ, ENV), patch("core.store_facts.requests.get", return_value=Resp(200, facts("8-10"))):
            store_facts.fetch_store_facts()
        store_facts.cache.delete(store_facts._FRESH)          # the TTL ran out

        new_worker(store_facts)
        boom = store_facts.requests.RequestException("down")
        with patch.dict(os.environ, ENV), patch("core.store_facts.requests.get", side_effect=boom):
            got = store_facts.fetch_store_facts()
        self.assertEqual(got["stores"]["yakima"]["hours"], "8-10")

    def test_a_cache_outage_never_raises_out_of_fetch_store_facts(self):
        broken = MagicMock()
        broken.get.side_effect = ConnectionError("redis down")
        broken.set.side_effect = ConnectionError("redis down")
        broken.delete.side_effect = ConnectionError("redis down")
        with patch.dict(os.environ, ENV), patch("core.store_facts.cache", broken), patch(
            "core.store_facts.requests.get", return_value=Resp(200, facts("8-10"))
        ):
            got = store_facts.fetch_store_facts()
            store_facts.invalidate()
        self.assertEqual(got["stores"]["yakima"]["hours"], "8-10")
