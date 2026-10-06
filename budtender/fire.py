"""Fire-and-forget a Celery task from a request without a dead broker freezing the request.

`task.delay()` retries the broker connection for ~70 s before it fails, so one outage froze every
search. Here the publish never retries (`retry=False`), and after one failure a circuit breaker keeps
requests from even trying for a minute. Never raises; returns whether the task was handed over.
"""
from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

BREAKER_KEY = "celery:broker_down"
BREAKER_SECONDS = 60


def fire(task, *args) -> bool:
    try:
        if cache.get(BREAKER_KEY):
            return False
        task.apply_async(args=list(args), retry=False)
        return True
    except Exception:  # noqa: BLE001 - a down broker must not fail or slow the request
        try:
            cache.set(BREAKER_KEY, 1, BREAKER_SECONDS)
        except Exception:  # noqa: BLE001
            pass
        logger.warning("could not enqueue %s (broker down?)", getattr(task, "name", task), exc_info=True)
        return False
