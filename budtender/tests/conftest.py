"""Per-test isolation for the budtender suite.

The chat-turn caps, the persona cache and the store-facts cache all live in Django's
cache, which under test is one process-global LocMemCache. Without a clear between
tests, budget or cached values left by one file leak into the next one.
"""
import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _isolate_cache():
    cache.clear()
    yield
    cache.clear()
