"""Fixed-window caps keyed on WHAT is being abused, not on where the request came from.

`pos_core.ratelimit` keys on client IP, and through the happytimeweed.com rewrite every
shopper reaches Django from Vercel's egress, so an IP bucket is one bucket for the whole
site. These caps key on the thing itself — a phone, an email — or on the endpoint as a
whole. Keys carry a keyed hash, never the raw value: the cache is no place for PII.
"""
from __future__ import annotations

from django.core.cache import cache
from django.utils.crypto import salted_hmac


def _key(scope: str, value: str) -> str:
    if not value:
        return f"cap:{scope}"
    return f"cap:{scope}:{salted_hmac('bundles.caps', value, algorithm='sha256').hexdigest()}"


def take(scope: str, limit: int, window: int, value: str = "") -> bool:
    """Spend one unit of `scope`'s budget (per `value`, or site-wide when blank).

    Atomic increment first, compare second: checking and then incrementing lets a burst
    of parallel requests all read "under the cap" before any of them counts. A refused
    take is handed straight back, so the counter always equals the units really spent.
    """
    key = _key(scope, value)
    cache.add(key, 0, window)
    try:
        used = cache.incr(key)
    except ValueError:  # expired between add and incr
        cache.set(key, 1, window)
        used = 1
    if used > limit:
        give_back(scope, value)
        return False
    return True


def give_back(scope: str, value: str = "") -> None:
    """Undo one `take` for an attempt that never became the thing being capped."""
    try:
        cache.decr(_key(scope, value))
    except ValueError:
        pass
