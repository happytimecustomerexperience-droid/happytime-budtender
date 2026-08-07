"""Per-test isolation for the POS suite.

The rate limiter (`pos_core.ratelimit`) counts into Django's cache, which under test
is a process-global LocMemCache keyed only on `(scope, ip)` — and every test client
shares the same `127.0.0.1`. So budget SPENT BY ONE TEST FILE IS GONE FOR THE NEXT
ONE. Files run alphabetically, so adding a handful of request-making tests to
`test_station_shop.py` silently pushed `test_views.py`'s begin-gate tests over the
`start` limit and they began returning 429 — passing alone, failing in the suite,
with nothing in the failure pointing at the real cause.

Clearing per test makes throttle tests deterministic (they prime their own bucket)
and stops any future file from starving the ones after it.
"""
import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _isolate_rate_limit_buckets():
    cache.clear()
    yield
    cache.clear()
