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

import json
import logging
import os

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure, task_prerun, task_success

logger = logging.getLogger(__name__)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("happytime_voice")
# Read CELERY_* settings off Django settings (namespace="CELERY"), then discover ``<app>/tasks.py``.
app.config_from_object("django.conf:settings", namespace="CELERY")
# Explicit package list (more reliable than the bare autodiscover under a worker boot) — the task
# modules are ``voice.tasks`` and ``kb.tasks``; add new ``<app>.tasks`` modules here.
app.autodiscover_tasks(["voice", "kb"])

# Beat schedule (requires a `celery -A core beat` process; a no-op unless one is running). Nightly
# check that the public site (its own Supabase CMS, not ours) still agrees with the KB — alerts
# staff on drift (P6; voice.tasks.check_store_facts_nightly).
app.conf.beat_schedule = {
    "check-store-facts-nightly": {
        "task": "voice.check_store_facts_nightly",
        "schedule": crontab(hour=3, minute=0),  # 3am Pacific — low-traffic window
    },
    # Mirror each store's current Dutchie deals into the Specials & hours rows. A no-op unless the
    # "Sync deals from Dutchie" capability (auto.deals_sync) is on.
    "sync-deals-from-dutchie": {
        "task": "kb.sync_deals",
        "schedule": crontab(minute="*/30"),
    },
}


@task_prerun.connect
def _refresh_dashboard_credentials(*args, **kwargs):
    """The worker is a separate process from the web workers: a credential saved on the dashboard
    (the staff alert email, the n8n/Slack URLs) reaches it only through this. A cheap cache.get
    unless the shared version token changed since this worker last applied the stored rows."""
    from dashboard import credentials

    credentials.refresh_if_stale()


# ── job reporting: every beat-scheduled run lands on the dashboard Health page (JobRun) ──────────
# Prerun/success/failure all fire in the process that executes the task, so a per-process dict
# links a run's start to its end. Only tasks named in ``beat_schedule`` are recorded (no second
# list to keep in step); recording is best-effort and never raises into the task.
_OPEN_RUNS: dict[str, int] = {}  # task id -> JobRun pk, for beat tasks running in this process


def _job_outcome(result) -> tuple[bool, str]:
    """(ok, summary) for a task that returned. ``{"skipped": ...}`` (a capability switched off, a
    store unreachable) is not a failure — it is recorded ok with the reason."""
    if isinstance(result, dict):
        if "skipped" in result:
            return True, f"skipped: {result['skipped']}"
        return True, json.dumps(result, default=str, sort_keys=True)
    return True, "" if result is None else str(result)


def _is_beat_task(task) -> bool:
    return task is not None and task.name in {e["task"] for e in app.conf.beat_schedule.values()}


def _job_run_end(task, task_id, ok: bool, summary: str) -> None:
    """Close the run opened at prerun; if the start was never recorded (a transient DB error),
    still report the end."""
    try:
        if not _is_beat_task(task):
            return
        from dashboard.models import JobRun

        pk = _OPEN_RUNS.pop(task_id, None)
        if pk is None:
            JobRun.record(task.name, ok=ok, summary=summary, source="beat")
        else:
            JobRun.objects.get(pk=pk).finish(ok, summary)
    except Exception:  # noqa: BLE001 - reporting must never break the job it reports on
        logger.warning("job run end not recorded", exc_info=True)


@task_prerun.connect
def _job_run_started(sender=None, task_id=None, task=None, **kwargs):
    try:
        if _is_beat_task(task):
            from dashboard.models import JobRun

            _OPEN_RUNS[task_id] = JobRun.begin(task.name, "beat").pk
    except Exception:  # noqa: BLE001 - reporting must never break the job it reports on
        logger.warning("job run start not recorded", exc_info=True)


@task_success.connect
def _job_run_succeeded(sender=None, result=None, **kwargs):
    try:
        _job_run_end(sender, sender.request.id, *_job_outcome(result))
    except Exception:  # noqa: BLE001 - e.g. a result that will not serialize
        logger.warning("job run end not recorded", exc_info=True)


@task_failure.connect
def _job_run_failed(sender=None, task_id=None, exception=None, **kwargs):
    _job_run_end(sender, task_id, False, f"{type(exception).__name__}: {exception}"[:500])


@app.task(bind=True, ignore_result=True)
def debug_task(self):  # pragma: no cover - operational smoke task
    """A trivial smoke task (mirrors budtender) to verify a worker is alive."""
    return f"request: {self.request!r}"
