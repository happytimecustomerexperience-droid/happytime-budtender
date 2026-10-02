"""The dashboard Health page — one honest row per background job.

Every job reports its runs into ``JobRun``: the Celery beat tasks through the signal handlers in
``core/celery.py``, the host cron scripts through ``manage.py record_job_run``. This page reads
them back so a job that fails, or silently stops running, is visible instead of living in a log
nobody opens. STALE = the last finish is older than the job's expected interval (beat: 2x its
schedule interval, read from the beat schedule — no second list; host cron jobs: 26 h).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from celery.schedules import crontab
from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render
from django.utils import timezone
from django.utils.timesince import timesince

from .models import JobRun

PACIFIC = ZoneInfo("America/Los_Angeles")
BEAT_STALE_FACTOR = 2
# Host cron jobs (they report through ``record_job_run``): a daily job that has not finished in
# 26 h missed a night. The crontab line for each lives in the script's header.
CRON_JOBS = {
    "daily-maintenance": timedelta(hours=26),
    "customers-refresh": timedelta(hours=26),
}


def schedule_interval(schedule) -> timedelta | None:
    """The longest gap between two runs of a beat schedule, or None when it can't be told (a
    crontab restricted to certain weekdays/days/months) — such a job is never judged STALE."""
    if isinstance(schedule, timedelta):
        return schedule
    if isinstance(schedule, int | float):
        return timedelta(seconds=schedule)
    if hasattr(schedule, "run_every"):
        return schedule.run_every
    if isinstance(schedule, crontab):
        if (
            schedule.day_of_week != set(range(7))
            or schedule.day_of_month != set(range(1, 32))
            or schedule.month_of_year != set(range(1, 13))
        ):
            return None
        starts = sorted(h * 60 + m for h in schedule.hour for m in schedule.minute)
        gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
        gaps.append(starts[0] + 24 * 60 - starts[-1])  # the wrap to tomorrow's first run
        return timedelta(minutes=max(gaps))
    return None


def expected_jobs() -> dict[str, dict]:
    """{job name: {"source": "beat"|"cron", "window": timedelta|None}} — the beat tasks read from
    the beat schedule, plus the host cron jobs."""
    from core.celery import app

    jobs: dict[str, dict] = {}
    for entry in app.conf.beat_schedule.values():
        interval = schedule_interval(entry["schedule"])
        window = None if interval is None else interval * BEAT_STALE_FACTOR
        name = entry["task"]
        if name in jobs and window is not None and jobs[name]["window"] is not None:
            window = min(window, jobs[name]["window"])
        jobs[name] = {"source": "beat", "window": window}
    for name, window in CRON_JOBS.items():
        jobs[name] = {"source": "cron", "window": window}
    return jobs


def _pacific(dt: datetime | None) -> str:
    return dt.astimezone(PACIFIC).strftime("%Y-%m-%d %H:%M %Z") if dt else ""


def _span(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60} h" if minutes % 60 == 0 and minutes >= 120 else f"{minutes} min"


def job_rows(now: datetime | None = None) -> list[dict]:
    """One dict per job: expected jobs first (a never-reported one says so), then any other name
    that has reported."""
    now = now or timezone.now()
    expected = expected_jobs()
    by_name: dict[str, list[JobRun]] = {}
    for run in JobRun.objects.all():  # newest first (Meta.ordering); <= JobRun.KEEP rows per name
        by_name.setdefault(run.name, []).append(run)
    names = list(expected) + sorted(n for n in by_name if n not in expected)

    rows = []
    for name in names:
        runs = by_name.get(name, [])
        spec = expected.get(name, {"source": "", "window": None})
        window = spec["window"]
        finished = next((r for r in runs if r.finished_at is not None), None)
        running = bool(runs) and runs[0].finished_at is None
        ref = finished.finished_at if finished else (runs[0].started_at if runs else None)
        if not runs:
            status = "no report yet"
        elif finished is None:
            status = "running"
        else:
            status = "ok" if finished.ok else "FAILED"
        rows.append(
            {
                "name": name,
                "source": spec["source"] or (runs[0].source if runs else ""),
                "status": status,
                "running": running and finished is not None,  # a newer run is in flight
                "stale": window is not None and ref is not None and now - ref > window,
                "stale_after": _span(window) if window else "",
                "when": _pacific(ref),
                "age": f"{timesince(ref, now)} ago" if ref else "",
                "summary": finished.summary if finished else "",
            }
        )
    return rows


def freshness(now: datetime | None = None) -> list[dict]:
    """How fresh the data the jobs feed is, read from the data itself: the newest Dutchie deal row
    and the newest customer profile."""
    from crm.models import CustomerProfile
    from kb.models import StoreFact

    now = now or timezone.now()
    deal = StoreFact.objects.filter(label__startswith="Dutchie #").order_by("-updated_at").first()
    deal_at = deal.updated_at if deal else None
    customer_at = CustomerProfile.objects.order_by("-updated_at").values_list("updated_at", flat=True).first()
    return [
        {
            "name": "Dutchie deals (newest synced row)",
            "detail": deal.label if deal else "",
            "when": _pacific(deal_at),
            "age": f"{timesince(deal_at, now)} ago" if deal_at else "",
        },
        {
            "name": "Customer profiles (newest update)",
            "detail": "",
            "when": _pacific(customer_at),
            "age": f"{timesince(customer_at, now)} ago" if customer_at else "",
        },
    ]


@staff_member_required
def health(request):
    return render(
        request,
        "dashboard/health.html",
        {"jobs": job_rows(), "freshness": freshness()},
    )
