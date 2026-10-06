"""ONE lock for everything that spends Dutchie's backoffice call budget (60/min per login, pacing is only
per process): the New Drops refresh, the lab / product-detail warm and the on-demand warm all take it, so
two of them never run at once.

The lock's VALUE is an owner token. `release` deletes the key only while it still holds the caller's own
token: a run that outlived the TTL must not delete the lock of the run that took over after it.
"""
from __future__ import annotations

import secrets

from django.core.cache import cache

KEY = "dutchie:backoffice:lock"


def acquire(ttl: int) -> str | None:
    """The owner token if the lock was free (now held for up to `ttl` seconds), else None."""
    token = secrets.token_hex(8)
    return token if cache.add(KEY, token, ttl) else None


def release(token: str | None) -> None:
    """Free the lock, but only if `token` still owns it (get-then-delete: a cache has no atomic
    compare-and-delete; the window is far smaller than the 50-minute TTL this guards against)."""
    if token and cache.get(KEY) == token:
        cache.delete(KEY)


def is_held() -> bool:
    return bool(cache.get(KEY))
