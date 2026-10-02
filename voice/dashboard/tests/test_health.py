"""The Health page and the job-run reporting behind it (JobRun).

Beat tasks report through the Celery signal handlers in ``core/celery.py``; host scripts through
``manage.py record_job_run``; ``/dashboard/health/`` reads them back. Offline, SQLite.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.contrib.auth.models import User
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from dashboard import health, monitor
from dashboard.models import JobRun

BEAT_TASK = "kb.sync_deals"  # in core/celery.py's beat schedule


@pytest.fixture
def staff(db):
    return User.objects.create_user("boss-health", password="x", is_staff=True)


@pytest.fixture
def client_staff(client, staff):
    client.force_login(staff)
    return client


def _run_beat_task(monkeypatch, body, *, throw=None):
    """Run kb.sync_deals eagerly with ``body`` as the deals sync. (Test settings make eager runs
    re-raise, and Celery then skips its failure signal — ``throw=False`` runs it like a worker.)"""
    from kb.tasks import sync_deals

    monkeypatch.setattr("kb.deals_sync.sync_deals", body)
    return sync_deals.apply(throw=throw)


# ── beat tasks → JobRun (Celery signals) ───────────────────────────────────────
@pytest.mark.django_db
def test_beat_task_success_is_recorded_ok(monkeypatch):
    result = _run_beat_task(monkeypatch, lambda: {"yakima": {"created": 2, "updated": 0}})

    assert result.successful()
    row = JobRun.objects.get(name=BEAT_TASK)
    assert (row.ok, row.source) == (True, "beat")
    assert row.finished_at is not None and "yakima" in row.summary


@pytest.mark.django_db
def test_beat_task_failure_is_recorded_failed(monkeypatch):
    def boom():
        raise RuntimeError("dutchie said 429")

    result = _run_beat_task(monkeypatch, boom, throw=False)

    assert result.failed()
    row = JobRun.objects.get(name=BEAT_TASK)
    assert row.ok is False and row.finished_at is not None
    assert "RuntimeError: dutchie said 429" in row.summary


@pytest.mark.django_db
def test_skipped_result_is_ok_not_a_failure(monkeypatch):
    _run_beat_task(monkeypatch, lambda: {"skipped": "capability off"})

    row = JobRun.objects.get(name=BEAT_TASK)
    assert row.ok is True
    assert row.summary == "skipped: capability off"


@pytest.mark.django_db
def test_only_beat_scheduled_tasks_are_recorded():
    from voice.tasks import rollup_analytics

    rollup_analytics.apply()  # a real task, but not in the beat schedule
    assert not JobRun.objects.exists()


@pytest.mark.django_db
def test_recording_never_raises_into_the_task(monkeypatch):
    def db_down(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(JobRun, "begin", db_down)
    monkeypatch.setattr(JobRun, "record", db_down)
    assert _run_beat_task(monkeypatch, lambda: {"yakima": {}}).successful()  # start + end both fail

    monkeypatch.undo()
    monkeypatch.setattr(JobRun, "finish", db_down)
    assert _run_beat_task(monkeypatch, lambda: {"yakima": {}}).successful()  # only the end fails
    assert JobRun.objects.get(name=BEAT_TASK).ok is None  # started, never closed -> shows "running"


@pytest.mark.django_db
def test_failing_start_still_reports_the_end(monkeypatch):
    def db_hiccup(*a, **kw):
        raise RuntimeError("transient")

    monkeypatch.setattr(JobRun, "begin", db_hiccup)
    _run_beat_task(monkeypatch, lambda: {"yakima": {}})

    assert JobRun.objects.get(name=BEAT_TASK).ok is True  # the end was recorded on its own


# ── JobRun keeps the last 50 per name ──────────────────────────────────────────
@pytest.mark.django_db
def test_jobrun_keeps_only_the_newest_fifty_per_name():
    base = timezone.now() - timedelta(days=100)
    for i in range(55):
        JobRun.objects.create(name="a-job", source="cron", started_at=base + timedelta(hours=i))
    JobRun.objects.create(name="other-job", source="cron")

    JobRun.record("a-job", ok=True, summary="newest", source="cron")  # the write that prunes

    kept = JobRun.objects.filter(name="a-job")
    assert kept.count() == JobRun.KEEP
    assert kept.filter(summary="newest").exists()
    assert not kept.filter(started_at=base).exists()  # the oldest went
    assert JobRun.objects.filter(name="other-job").count() == 1  # another name is untouched


# ── manage.py record_job_run ───────────────────────────────────────────────────
@pytest.mark.django_db
def test_record_job_run_ok_and_fail():
    call_command("record_job_run", "daily-maintenance", "--ok", "--summary", "all steps ok", "--source", "cron", stdout=StringIO())
    call_command("record_job_run", "daily-maintenance", "--fail", "--summary", "FAILED: scrape site into KB", stdout=StringIO())

    ok_row, fail_row = JobRun.objects.filter(name="daily-maintenance").order_by("id")
    assert (ok_row.ok, ok_row.source, ok_row.summary) == (True, "cron", "all steps ok")
    assert (fail_row.ok, fail_row.source, fail_row.summary) == (False, "manual", "FAILED: scrape site into KB")
    assert ok_row.finished_at is not None


@pytest.mark.django_db
def test_record_job_run_needs_exactly_one_outcome_and_a_sane_name():
    with pytest.raises(CommandError):
        call_command("record_job_run", "daily-maintenance")  # neither --ok nor --fail
    with pytest.raises(CommandError):
        call_command("record_job_run", "daily-maintenance", "--ok", "--fail")
    with pytest.raises(CommandError):
        call_command("record_job_run", "x" * 65, "--ok")
    assert not JobRun.objects.exists()


# ── the Health page ────────────────────────────────────────────────────────────
def _row(html: str, name: str) -> str:
    start = html.index(f'data-testid="job-row-{name}"')
    return html[start : html.index("</tr>", start)]


@pytest.mark.django_db
def test_health_page_is_staff_only(client, staff):
    from django.test import Client

    url = reverse("dash-health")
    assert Client().get(url).status_code == 302
    clerk = Client()
    clerk.force_login(User.objects.create_user("clerk-health", password="x"))  # not staff
    assert clerk.get(url).status_code == 302
    client.force_login(staff)
    assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_health_page_shows_failed_stale_ok_running_and_no_report_yet(client_staff):
    now = timezone.now()
    # failed, recently
    JobRun.record("daily-maintenance", ok=False, summary="FAILED: scrape site into KB", source="cron")
    # ok, but 30 h ago against a 26 h allowance -> STALE
    old = JobRun.record("customers-refresh", ok=True, summary="Imported 12 customer profiles", source="cron")
    JobRun.objects.filter(pk=old.pk).update(finished_at=now - timedelta(hours=30), started_at=now - timedelta(hours=30))
    # ok and fresh, with a newer run still in flight
    JobRun.record(BEAT_TASK, ok=True, summary="deals ok", source="beat")
    JobRun.begin(BEAT_TASK, "beat")

    html = client_staff.get(reverse("dash-health")).content.decode()

    maint = _row(html, "daily-maintenance")
    assert "FAILED" in maint and "FAILED: scrape site into KB" in maint and "STALE" not in maint
    cust = _row(html, "customers-refresh")
    assert "STALE" in cust and ">ok<" in cust and "Imported 12 customer profiles" in cust
    deals = _row(html, BEAT_TASK)
    assert "running now" in deals and "deals ok" in deals and "STALE" not in deals
    never = _row(html, "voice.check_store_facts_nightly")  # beat task that has never reported
    assert "no report yet" in never and "STALE" not in never


@pytest.mark.django_db
def test_stale_threshold_is_twice_the_beat_interval_and_26h_for_cron():
    now = timezone.now()

    def finished(name, hours_ago):
        run = JobRun.record(name, ok=True, source="beat")
        JobRun.objects.filter(pk=run.pk).update(finished_at=now - timedelta(hours=hours_ago))

    finished(BEAT_TASK, 0.9)  # every 30 min -> allowed 60 min: 54 min is fine
    finished("voice.check_store_facts_nightly", 47)  # daily 03:00 -> allowed 48 h
    finished("daily-maintenance", 25.9)
    by_name = {r["name"]: r for r in health.job_rows(now)}
    assert not any(by_name[n]["stale"] for n in (BEAT_TASK, "voice.check_store_facts_nightly", "daily-maintenance"))
    assert by_name[BEAT_TASK]["stale_after"] == "60 min"
    assert by_name["voice.check_store_facts_nightly"]["stale_after"] == "48 h"

    finished(BEAT_TASK, 1.1)
    finished("voice.check_store_facts_nightly", 49)
    finished("daily-maintenance", 26.1)
    by_name = {r["name"]: r for r in health.job_rows(now)}
    assert all(by_name[n]["stale"] for n in (BEAT_TASK, "voice.check_store_facts_nightly", "daily-maintenance"))


@pytest.mark.django_db
def test_pacific_time_and_data_freshness(client_staff):
    from crm.models import CustomerProfile
    from kb.models import StoreFact

    at = timezone.now().replace(year=2026, month=7, day=1, hour=19, minute=30, second=0, microsecond=0)
    JobRun.record("daily-maintenance", ok=True, source="cron")
    JobRun.objects.update(finished_at=at)
    fact = StoreFact.objects.create(store="yakima", kind="special", label="Dutchie #4242", value="x")
    StoreFact.objects.create(store="yakima", kind="special", label="Owner special", value="y")
    StoreFact.objects.filter(pk=fact.pk).update(updated_at=at)
    CustomerProfile.objects.create(customer_key="cust-h")
    CustomerProfile.objects.update(updated_at=at)

    html = client_staff.get(reverse("dash-health")).content.decode()

    assert "2026-07-01 12:30 PDT" in _row(html, "daily-maintenance")  # 19:30 UTC is 12:30 Pacific
    fresh = html[html.index('data-testid="health-freshness"') :]
    assert fresh.count("2026-07-01 12:30 PDT") == 2 and "Dutchie #4242" in fresh
    assert "Owner special" not in fresh


@pytest.mark.django_db
def test_data_freshness_says_none_when_the_tables_are_empty(client_staff):
    html = client_staff.get(reverse("dash-health")).content.decode()
    assert html.count('<span class="badge slate">none</span>') == 2


# ── calls monitor: a blank-outcome call past the window is "ended", not live ────
@pytest.mark.django_db
def test_blank_outcome_call_past_two_hours_is_ended_not_live():
    from voice.models import VoiceCall

    fresh = VoiceCall.objects.create(call_id="m-fresh")
    stale = VoiceCall.objects.create(call_id="m-stale")
    reported = VoiceCall.objects.create(call_id="m-done", outcome="faq_answered")
    old = timezone.now() - timedelta(hours=3)
    VoiceCall.objects.filter(pk__in=[stale.pk, reported.pk]).update(created_at=old)
    stale.refresh_from_db()
    reported.refresh_from_db()

    assert monitor.is_live(fresh) and not monitor.is_live(stale) and not monitor.is_live(reported)
    assert monitor.call_status_badge(fresh) == ("In progress", "slate")
    assert monitor.call_status_badge(stale) == ("Ended (no report)", "amber")
    assert monitor.call_status_badge(reported) == ("FAQ answered", "green")
    assert list(monitor.live_calls()) == [fresh]  # the queryset agrees with the predicate


@pytest.mark.django_db
def test_call_pages_label_an_unreported_old_call_ended(client_staff):
    from voice.models import VoiceCall

    stale = VoiceCall.objects.create(call_id="m-stale-page")
    VoiceCall.objects.filter(pk=stale.pk).update(created_at=timezone.now() - timedelta(hours=5))

    for name, args in (("dash-call-log", []), ("dash-call-detail", [stale.pk]), ("dash-conversation-history", [])):
        html = client_staff.get(reverse(name, args=args)).content.decode().lower()
        assert "ended (no report)" in html, name
        assert "in progress" not in html and "in flight" not in html, name
