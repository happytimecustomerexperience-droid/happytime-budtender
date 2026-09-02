"""Keeps voice/voice/pricing.py's OTD rule in lockstep with bundles/tax.py.

The voice project cannot import this root package (its Docker build context is `voice/`
only — see `voice/Dockerfile` / `docker-compose*.yaml`, both `build: ./voice`), so
`voice/voice/pricing.py` re-states "the menu price IS the price" as identity-with-rounding
instead of importing `bundles.tax.quote()`. Two independent statements of one rule can drift
silently; this test loads the voice module by file path and asserts the two never disagree.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from django.test import SimpleTestCase

from bundles import tax

_PRICING_PATH = Path(__file__).resolve().parents[2] / "voice" / "voice" / "pricing.py"


def _load_pricing():
    spec = importlib.util.spec_from_file_location("_voice_pricing_lockstep", _PRICING_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PricingLockstepTests(SimpleTestCase):
    def test_otd_matches_bundles_tax_total(self):
        pricing = _load_pricing()
        for price in (0, 8, 22.5, 38, 67.13):
            self.assertEqual(
                pricing.otd(price),
                float(tax.quote(price, "yakima")["total"]),
                f"voice/voice/pricing.otd({price}) drifted from bundles.tax.quote(...)['total']",
            )
