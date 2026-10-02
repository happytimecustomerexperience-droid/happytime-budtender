"""Live store facts (hours/address/phone) fetched from the voice service.

Pure module, no Django models — mirrors the fetch/cache/stale-on-failure shape of
``budtender.gemini_chat.fetch_persona()`` so the two live-data bridges behave the
same way under an unreachable voice service: never raise, serve stale cache, warn
at most once per TTL window. The cache is Django's (Redis in prod), shared by every
gunicorn worker, so a dashboard "refresh" reaches all of them.
"""
from __future__ import annotations

import logging
import os
import time

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Same header pattern as budtender.gemini_chat._VOICE_HEADERS — the voice service
# runs with SECURE_SSL_REDIRECT on, so a plain http:// call to the internal
# container name 301s to a port nothing listens on. X-Forwarded-Proto tells Django
# the hop was already secure.
_VOICE_HEADERS = {"Accept": "application/json", "X-Forwarded-Proto": "https"}

# Three shared keys (see budtender.gemini_chat for the same shape): the value with the TTL, a
# copy that outlives it (stale beats none while the voice host is down), and a back-off marker.
_FRESH = "core:store_facts:fresh:v1"
_LAST_GOOD = "core:store_facts:last-good:v1"
_FAILED = "core:store_facts:failed:v1"
_LAST_GOOD_TTL = 7 * 24 * 3600
_warned_at: float | None = None   # per-process log throttle only, never data


def _cache_get(key):
    try:
        return cache.get(key)
    except Exception:  # noqa: BLE001 - a cache outage degrades to "miss", never an error
        logger.warning("store_facts: cache read failed", exc_info=True)
        return None


def _cache_set(key, value, timeout) -> None:
    try:
        cache.set(key, value, timeout)
    except Exception:  # noqa: BLE001
        logger.warning("store_facts: cache write failed", exc_info=True)


def invalidate() -> None:
    """Drop the shared store facts so the next fetch hits the voice service (every worker)."""
    for key in (_FRESH, _LAST_GOOD, _FAILED):
        try:
            cache.delete(key)
        except Exception:  # noqa: BLE001
            logger.warning("store_facts: cache delete failed", exc_info=True)


def fetch_store_facts(*, force: bool = False) -> dict | None:
    """Fetch live store facts from the voice service, with a shared TTL cache.

    On success, caches and returns {"ok", "stores", "global", "updated_at"}. On
    failure, returns the last good cached value if any (stale is better than
    none), else None. Never raises.
    """
    global _warned_at

    ttl = int(os.environ.get("HHT_STORE_FACTS_TTL", "300"))
    now = time.monotonic()
    if not force:
        fresh = _cache_get(_FRESH)
        if fresh is not None:
            return fresh
        # Back off after a failure: every storefront render calls this, and an unreachable voice
        # host would otherwise cost a connect timeout per page view (and hang the test suite).
        if _cache_get(_FAILED):
            return _cache_get(_LAST_GOOD)
    cached = _cache_get(_LAST_GOOD)

    base = os.environ.get("HHT_VOICE_BASE_URL", "").rstrip("/")
    token = os.environ.get("HHT_BACKEND_TOKEN", "").strip()
    if base and token:
        try:
            resp = requests.get(
                f"{base}/api/voice/store-facts",
                headers={**_VOICE_HEADERS, "Authorization": f"Bearer {token}"},
                timeout=(2.0, float(os.environ.get("HHT_VOICE_TIMEOUT", "5") or 5)),
            )
            data = resp.json() if resp.status_code < 300 and resp.content else None
        except (requests.RequestException, ValueError):
            data = None
        if isinstance(data, dict) and data.get("ok") and isinstance(data.get("stores"), dict):
            _cache_set(_FRESH, data, ttl)
            _cache_set(_LAST_GOOD, data, _LAST_GOOD_TTL)
            _warned_at = None
            logger.info("store_facts: using live voice KB (updated %s)", data.get("updated_at"))
            return data

    _cache_set(_FAILED, 1, int(os.environ.get("HHT_STORE_FACTS_RETRY", "60")))
    if cached is not None:
        return cached
    if _warned_at is None or now - _warned_at >= ttl:
        logger.warning("store_facts: voice service unreachable, using static fallback")
        _warned_at = now
    return None
