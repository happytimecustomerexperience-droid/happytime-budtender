"""Customer-360 — the taste profile + product enrichment the POS reads out of the
shared budtender database.

Before the merge this connected to happytime over a raw ``psycopg`` link
(``CUSTOMER_DB_DSN``). Now it's one app + one Postgres, so the readers are plain
ORM (``budtender.reads``). This module keeps the historical
``customers.intelligence.*`` import surface and owns the profile cache wrapper so
it honours a monkeypatched ``load_profile_full`` in tests.
"""
import re

from django.core.cache import cache

from budtender.reads import (  # noqa: F401 — re-export the shared ORM readers
    _HHT_LOC,
    _MISS,
    _PROFILE_TTL,
    _phone_candidates,
    _top,
    load_all_profiles,
    load_customer_history,
    load_product_enrichment,
    load_profile_full,
)


def load_profile_full_cached(phone, ttl=_PROFILE_TTL):
    """Cached ``load_profile_full``. The personalized menu re-renders on EVERY
    filter change, so cache the taste profile (negatives too, so an empty profile
    isn't re-queried per keystroke). A stampede lock keeps concurrent cold
    requests from stacking DB round-trips; cache failures fall through to a live
    read — never breaks the page. Calls the module-level ``load_profile_full`` so
    tests can monkeypatch it."""
    if not phone:
        return None
    key = f"prof:{re.sub(r'[^0-9+]', '', phone)}"
    try:
        hit = cache.get(key, _MISS)
        if hit is not _MISS:
            return hit
        if not cache.add(f"{key}:lock", "1", 10):
            return None
    except Exception:
        return load_profile_full(phone)
    try:
        val = load_profile_full(phone)
        cache.set(key, val, ttl)
        return val
    finally:
        try:
            cache.delete(f"{key}:lock")
        except Exception:
            pass
