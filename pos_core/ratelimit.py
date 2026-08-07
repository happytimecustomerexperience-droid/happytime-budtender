"""Tiny cache-backed rate limiter — scalable throttle for the public-ish endpoints.

Uses Django's cache (LocMemCache by default; swap to Redis/Memcached in prod for
multi-process correctness). ponytail: fixed-window counter, good enough; upgrade to a
token bucket only if abuse shows up.
"""

from __future__ import annotations

import functools
import time

from django.core.cache import cache
from django.http import HttpResponse


def _client_ip(request) -> str:
    # Use the LAST X-Forwarded-For hop (appended by our own Traefik) — the first
    # hops are client-supplied and spoofable, which would let an attacker dodge
    # the throttle by rotating fake XFF values.
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (xff.split(",")[-1].strip() if xff else request.META.get("REMOTE_ADDR", "")) or "anon"


def rate_limit(scope: str, limit: int = 30, window: int = 60, methods: tuple | None = None):
    """Allow `limit` requests per `window` seconds per (scope, ip). 429 when exceeded.

    `methods` limits WHICH verbs consume budget. Reading a page should not spend the
    allowance for doing the thing: /custom-order/checkout answers both GET (render the
    form) and POST (place the order), and counting the GET meant the header Cart button
    burned order budget — a shopper could be refused having never pressed submit.
    """
    def deco(view):
        @functools.wraps(view)
        def wrapped(request, *a, **kw):
            if methods is not None and request.method not in methods:
                return view(request, *a, **kw)
            bucket = int(time.time() // window)
            key = f"rl:{scope}:{_client_ip(request)}:{bucket}"
            try:
                n = cache.get_or_set(key, 0, timeout=window)
                cache.incr(key)
            except ValueError:  # key expired between get_or_set and incr
                cache.set(key, 1, timeout=window)
                n = 0
            if n >= limit:
                return HttpResponse("rate limit exceeded — slow down", status=429)
            return view(request, *a, **kw)
        return wrapped
    return deco
