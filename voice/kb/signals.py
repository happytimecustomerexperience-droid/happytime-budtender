"""P6 instant-refresh chain: a dashboard/admin/shell save is the ONLY source of truth, and every
downstream system follows automatically (no separate "reindex" or "publish" step to remember).

* ``StoreFact`` save/delete → nudge root's ``POST /api/v1/store-facts/refresh``.
* ``AgentPrompt`` save → ``dashboard.publish.auto_publish_on_save`` (the single call site — this
  used to also be called directly from ``dashboard/views.py``; that direct call is removed so a
  save publishes exactly once regardless of path) + nudge root's ``POST /api/v1/persona/refresh``.
  The publish status is left on ``instance.publish_note`` for the save toast.
* ``BotCapability`` save (a switch on /dashboard/capabilities/) → the same publish for every
  active phone member (a switch changes their toolIds / transfer tool) + the persona nudge. The
  joined status is left on ``instance.publish_note``.

Both root nudges are best-effort (queued via Celery when wired, else inline — see
``voice.tasks.dispatch_budtender_notify``) and never raise into the save.

Registered in ``kb/apps.py::KbConfig.ready()``.
"""

from __future__ import annotations

import contextlib

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from dashboard.models import BotCapability
from kb.models import AgentPrompt, StoreFact

_suppress_depth = 0


@contextlib.contextmanager
def bulk():
    """Hold the per-row nudges during a bulk write (the boot-time ``seed_all``, an import) and
    send ONE store-facts nudge, ONE persona nudge, and one hash-gated publish per active prompt
    when the block ends — instead of ~80 POSTs and six Vapi PATCHes per deploy."""
    global _suppress_depth
    _suppress_depth += 1
    try:
        yield
    finally:
        _suppress_depth -= 1
        if _suppress_depth == 0:
            from dashboard.publish import auto_publish_on_save
            from voice import tasks

            for prompt in AgentPrompt.objects.filter(is_active=True):
                auto_publish_on_save(prompt)  # no-op when the row's publish hash is unchanged
            tasks.dispatch_budtender_notify("store-facts")
            tasks.dispatch_budtender_notify("persona")


@receiver(post_save, sender=StoreFact)
@receiver(post_delete, sender=StoreFact)
def _store_fact_changed(sender, **kwargs):
    if _suppress_depth:
        return
    from voice import tasks

    tasks.dispatch_budtender_notify("store-facts")


@receiver(post_save, sender=AgentPrompt)
def _agent_prompt_saved(sender, instance, **kwargs):
    if _suppress_depth:
        return
    from dashboard.publish import auto_publish_on_save
    from voice import tasks

    instance.publish_note = auto_publish_on_save(instance)
    tasks.dispatch_budtender_notify("persona")


@receiver(post_save, sender=BotCapability)
def _capability_saved(sender, instance, **kwargs):
    if _suppress_depth:
        return
    from django.core.cache import cache

    from dashboard.publish import MEMBER_ROLES, auto_publish_on_save
    from voice import capabilities, tasks

    # set_enabled clears the switch cache only after this save returns — clear it now, or the
    # publish below would read the old switch states.
    cache.delete(capabilities._CACHE_KEY)
    notes = []
    for prompt in AgentPrompt.objects.filter(is_active=True, role__in=MEMBER_ROLES):
        note = auto_publish_on_save(prompt)
        if note:
            notes.append(f"{prompt.role}: {note}")
    instance.publish_note = "; ".join(notes)
    tasks.dispatch_budtender_notify("persona")
