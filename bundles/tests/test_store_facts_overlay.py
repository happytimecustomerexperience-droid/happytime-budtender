"""store_info()/all_stores() overlay live voice-KB hours/phone/address onto the
static STORES fallback, and fall back cleanly when the voice service is down.
"""
import os
from unittest.mock import patch

from django.test import SimpleTestCase

from bundles import catalog
from core import store_facts

ENV = {
    "HHT_VOICE_BASE_URL": "http://voice.internal:8000",
    "HHT_BACKEND_TOKEN": "secret-token",
}

LIVE_FACTS = {
    "ok": True,
    "stores": {
        "yakima": {
            "hours": "8 AM – 10 PM daily",
            "address": "1315 N 1st St, Yakima, WA 98901",
            "phone": "(509) 571-9999",
        },
    },
    "global": {"payment": "cash or debit", "age": "21+", "pickup": "in-store only"},
    "updated_at": "2026-09-02T00:00:00Z",
}


class Resp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = b"{}"

    def json(self):
        return self._payload


class StoreFactsOverlayTests(SimpleTestCase):
    def setUp(self):
        store_facts.invalidate()
        self.addCleanup(store_facts.invalidate)

    def test_overlay_uses_live_hours_and_phone_when_voice_service_reachable(self):
        with patch.dict(os.environ, ENV), patch(
            "core.store_facts.requests.get", return_value=Resp(200, LIVE_FACTS)
        ):
            info = catalog.store_info("yakima")

        self.assertEqual(info["hours"], "8 AM – 10 PM daily")
        self.assertEqual(info["phone"], "(509) 571-9999")
        # Address splits cleanly into the static shape, so it's overlaid too.
        self.assertEqual(info["street"], "1315 N 1st St")
        self.assertEqual(info["city"], "Yakima, WA 98901")

    def test_falls_back_to_fully_static_values_when_voice_service_unreachable(self):
        with patch.dict(os.environ, ENV), patch(
            "core.store_facts.requests.get",
            side_effect=store_facts.requests.RequestException("boom"),
        ):
            info = catalog.store_info("yakima")

        static = catalog.STORES["yakima"]
        self.assertEqual(info["hours"], static["hours"])
        self.assertEqual(info["phone"], static["phone"])
        self.assertEqual(info["street"], static["street"])
        self.assertEqual(info["city"], static["city"])

    def test_unsplittable_live_address_keeps_static_street_and_city(self):
        messy = dict(LIVE_FACTS)
        messy["stores"] = dict(LIVE_FACTS["stores"])
        messy["stores"]["yakima"] = dict(LIVE_FACTS["stores"]["yakima"])
        messy["stores"]["yakima"]["address"] = "somewhere near the freeway"

        with patch.dict(os.environ, ENV), patch(
            "core.store_facts.requests.get", return_value=Resp(200, messy)
        ):
            info = catalog.store_info("yakima")

        static = catalog.STORES["yakima"]
        self.assertEqual(info["street"], static["street"])
        self.assertEqual(info["city"], static["city"])
        # Hours/phone still overlay independently of the address decision.
        self.assertEqual(info["hours"], "8 AM – 10 PM daily")

    def test_all_stores_falls_back_when_unreachable(self):
        with patch.dict(os.environ, ENV), patch(
            "core.store_facts.requests.get",
            side_effect=store_facts.requests.RequestException("boom"),
        ):
            stores = catalog.all_stores()

        self.assertEqual(len(stores), 3)
        self.assertEqual(stores[0]["slug"], "yakima")
