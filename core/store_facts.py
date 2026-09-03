"""Live store facts (hours/address/phone) fetched from the voice service.

Pure module, no Django models — mirrors the fetch/cache/stale-on-failure shape of
``budtender.gemini_chat.fetch_persona()`` so the two live-data bridges behave the
same way under an unreachable voice service: never raise, serve stale cache, warn
at most once per TTL window.
"""
from __future__ import annotations

import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

# Same header pattern as budtender.gemini_chat._VOICE_HEADERS — the voice service
# runs with SECURE_SSL_REDIRECT on, so a plain http:// call to the internal
# container name 301s to a port nothing listens on. X-Forwarded-Proto tells Django
# the hop was already secure.
_VOICE_HEADERS = {"Accept": "application/json", "X-Forwarded-Proto": "https"}

_cache: dict = {"value": None, "fetched_at": None, "failed_at": None}
_warned_at: float | None = None


def invalidate() -> None:
    """Clear the cached store facts so the next fetch hits the voice service."""
    _cache["value"] = None
    _cache["fetched_at"] = None
    _cache["failed_at"] = None


def fetch_store_facts(*, force: bool = False) -> dict | None:
    """Fetch live store facts from the voice service, with a TTL cache.

    On success, caches and returns {"ok", "stores", "global", "updated_at"}. On
    failure, returns the last good cached value if any (stale is better than
    none), else None. Never raises.
    """
    global _warned_at

    ttl = int(os.environ.get("HHT_STORE_FACTS_TTL", "300"))
    now = time.monotonic()
    cached = _cache["value"]
    fetched_at = _cache["fetched_at"]
    if not force and cached is not None and fetched_at is not None and now - fetched_at < ttl:
        return cached

    # Back off after a failure: every storefront render calls this, and an unreachable voice
    # host would otherwise cost a connect timeout per page view (and hang the test suite).
    retry_after = int(os.environ.get("HHT_STORE_FACTS_RETRY", "60"))
    failed_at = _cache["failed_at"]
    if not force and failed_at is not None and now - failed_at < retry_after:
        return cached

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
            _cache["value"] = data
            _cache["fetched_at"] = now
            _warned_at = None
            logger.info("store_facts: using live voice KB (updated %s)", data.get("updated_at"))
            return data

    _cache["failed_at"] = now
    if cached is not None:
        return cached
    if _warned_at is None or now - _warned_at >= ttl:
        logger.warning("store_facts: voice service unreachable, using static fallback")
        _warned_at = now
    return None
