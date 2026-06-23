"""Expose the Celery app at import time so ``@shared_task`` binds to it and ``autodiscover_tasks``
runs (standard Django+Celery pattern). Importing this opens NO broker connection — Celery connects
lazily on first publish / worker boot — so it is safe under pytest with no Redis (CELERY_TASK_
ALWAYS_EAGER is auto-on in the test settings). P5, ADR-021."""

from __future__ import annotations

from core.celery import app as celery_app

__all__ = ("celery_app",)
