"""The Celery app for post-call background work (P5, ADR-021; 15-P5 §3.5).

OPTIONAL + gated: the queue is OFF by default (``HHT_USE_CELERY=0`` → the P2 inline path). swedish-bot
had no Celery; budtender does — this mirrors ``happytime-budtender/core/celery.py`` (the proven
app/broker/autodiscover wiring), adapting only the app name + settings module.

Binding (ADR-017 / 15-P5 §3.5): the durable ``VoiceCall`` write stays SYNCHRONOUS in the eocr
handler — only the NON-critical post-call work (Gemini summary, staff email, analytics roll-up) is
moved off the webhook turn. When ``HHT_USE_CELERY`` is off the very same tasks run INLINE (the sync
fallback in ``voice.tasks``), so the record is never lost and the suite runs broker-free.

Importing this module never opens a broker connection (Celery connects lazily on first publish /
worker boot), so it is safe to import under pytest with no Redis.
"""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("happytime_voice")
# Read CELERY_* settings off Django settings (namespace="CELERY"), then discover ``<app>/tasks.py``.
app.config_from_object("django.conf:settings", namespace="CELERY")
# Explicit package list (more reliable than the bare autodiscover under a worker boot) — the only
# task module today is ``voice.tasks``; add new ``<app>.tasks`` modules here.
app.autodiscover_tasks(["voice"])


@app.task(bind=True, ignore_result=True)
def debug_task(self):  # pragma: no cover - operational smoke task
    """A trivial smoke task (mirrors budtender) to verify a worker is alive."""
    return f"request: {self.request!r}"
