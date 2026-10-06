"""Per-test isolation for the budtender suite.

The chat-turn caps, the persona cache and the store-facts cache all live in Django's
cache, which under test is one process-global LocMemCache. Without a clear between
tests, budget or cached values left by one file leak into the next one.

A request that shows a pick with no stored lab/detail enqueues a warm task. A test must
never reach a real broker (it would hang for seconds, and a worker on the other end would
call Dutchie), so the task is replaced by a recording mock for every test here.
"""
from unittest import mock

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _isolate_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _no_real_celery(monkeypatch):
    monkeypatch.setattr("budtender.tasks.warm_ids", mock.Mock(name="warm_ids"))
