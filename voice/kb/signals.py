"""P6 instant-refresh chain: a dashboard/admin/shell save is the ONLY source of truth, and every
downstream system follows automatically (no separate "reindex" or "publish" step to remember).

* ``StoreFact`` save/delete → nudge root's ``POST /api/v1/store-facts/refresh``.
* ``AgentPrompt`` save → ``dashboard.publish.auto_publish_on_save`` (the single call site — this
  used to also be called directly from ``dashboard/views.py``; that direct call is removed so a
  save publishes exactly once regardless of path) + nudge root's ``POST /api/v1/persona/refresh``.

Both root nudges are best-effort (queued via Celery when wired, else inline — see
``voice.tasks.dispatch_budtender_notify``) and never raise into the save.

Registered in ``kb/apps.py::KbConfig.ready()``.
"""

from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from kb.models import AgentPrompt, StoreFact


@receiver(post_save, sender=StoreFact)
@receiver(post_delete, sender=StoreFact)
def _store_fact_changed(sender, **kwargs):
    from voice import tasks

    tasks.dispatch_budtender_notify("store-facts")


@receiver(post_save, sender=AgentPrompt)
def _agent_prompt_saved(sender, instance, **kwargs):
    from dashboard.publish import auto_publish_on_save
    from voice import tasks

    auto_publish_on_save(instance)
    tasks.dispatch_budtender_notify("persona")
