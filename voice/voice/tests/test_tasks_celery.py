"""P5 #5 — the gated Celery post-call scaffold (15-P5 §3.5; §6 AC-5; §7.2).

Asserts: (1) the Celery app + the three tasks are IMPORTABLE offline (no broker); (2) the durable
``VoiceCall`` write happens SYNCHRONOUSLY in the eocr handler regardless of the flag (ADR-017 —
record never lost); (3) the SYNC FALLBACK runs the post-call work inline when HHT_USE_CELERY is off
(P2 behavior); (4) the tasks are idempotent (re-run = no dup). Tests are key-free/offline — Gemini
is the conftest mock, email is the console backend, no Redis (CELERY_TASK_ALWAYS_EAGER is on under
pytest). Expected values hand-authored (03-CONVENTIONS.md §5).
"""

from __future__ import annotations

import pytest


# ── (1) importable offline, no broker ───────────────────────────────────────────
def test_celery_app_importable_no_broker():
    """The Celery app imports without opening a broker connection (lazy connect)."""
    from core.celery import app

    assert app.main == "happytime_voice"


def test_tasks_importable():
    """The three post-call tasks import + are registered as Celery tasks."""
    from voice import tasks

    assert callable(tasks.summarize_call)
    assert callable(tasks.dispatch_alerts)
    assert callable(tasks.rollup_analytics)
    assert callable(tasks.run_post_call)


# ── (2) the durable write stays synchronous, flag on or off ─────────────────────
def _eocr_message(call_id: str = "call-celery-1") -> dict:
    return {
        "type": "end-of-call-report",
        "call": {"id": call_id, "customer": {"number": "+15095551212"}},
        "transcript": "Caller: what time do you close? Agent: nine to eleven.",
        "durationSeconds": 42,
        "messages": [],
    }


@pytest.mark.django_db
def test_durable_write_is_synchronous_inline(settings, mock_gemini):
    """AC-5: with the queue OFF, the eocr handler writes the VoiceCall row synchronously AND the
    sync fallback populates the summary inline (Gemini mock → 'OK')."""
    settings.HHT_USE_CELERY = False
    from voice import webhooks
    from voice.models import VoiceCall

    webhooks.handle_end_of_call_report(_eocr_message("call-sync-1"))
    vc = VoiceCall.objects.get(call_id="call-sync-1")
    assert vc.outcome  # durable record written
    assert vc.ai_summary == "OK"  # inline summary ran (sync fallback)


@pytest.mark.django_db
def test_durable_write_survives_when_queue_enabled(settings, mock_gemini):
    """AC-5: with the queue ON (eager under pytest), the durable VoiceCall row STILL exists after
    the handler returns — the record is written synchronously before any task runs."""
    settings.HHT_USE_CELERY = True
    from voice import webhooks
    from voice.models import VoiceCall

    webhooks.handle_end_of_call_report(_eocr_message("call-eager-1"))
    vc = VoiceCall.objects.get(call_id="call-eager-1")
    assert vc.outcome  # record present even with the queue path taken


# ── (3) sync fallback when broker enqueue fails ─────────────────────────────────
@pytest.mark.django_db
def test_enqueue_failure_degrades_to_inline(settings, mock_gemini, monkeypatch):
    """AC-5: HHT_USE_CELERY=1 but the broker .delay raises (Redis down) → run_post_call degrades to
    inline so the summary/email work is never silently dropped."""
    settings.HHT_USE_CELERY = True
    from voice import tasks
    from voice.models import VoiceCall

    vc = VoiceCall.objects.create(call_id="call-broker-down", store="yakima", transcript="hi")

    def _boom(*a, **k):
        raise RuntimeError("redis unreachable")

    monkeypatch.setattr(tasks.summarize_call, "delay", _boom)
    tasks.run_post_call(vc.pk)  # must not raise
    vc.refresh_from_db()
    assert vc.ai_summary == "OK"  # inline fallback ran the summary


# ── (4) idempotency: re-run never duplicates ────────────────────────────────────
@pytest.mark.django_db
def test_summarize_task_idempotent(settings, mock_gemini):
    """summarize_call is idempotent — a second run with a summary present is a no-op (no re-call)."""
    from voice import tasks
    from voice.models import VoiceCall

    vc = VoiceCall.objects.create(call_id="call-idem", store="yakima", transcript="hello there")
    first = tasks.summarize_call(vc.pk)
    assert first == "OK"
    # mutate the stored summary; a re-run must NOT overwrite it (idempotent short-circuit).
    vc.ai_summary = "already summarized"
    vc.save(update_fields=["ai_summary"])
    second = tasks.summarize_call(vc.pk)
    assert second == "already summarized"


@pytest.mark.django_db
def test_dispatch_alerts_idempotent_no_dup_email(settings):
    """dispatch_alerts re-run never re-sends — the AlertDelivery ledger short-circuits a delivered
    sink (no duplicate email on a Vapi re-delivery)."""
    settings.STAFF_ALERT_EMAIL = "staff@example.com"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    from django.core import mail

    from voice import tasks
    from voice.models import Outcome, VoiceCall

    vc = VoiceCall.objects.create(
        call_id="call-dispatch",
        store="yakima",
        outcome=Outcome.ESCALATION,
        reason="defective_return",
    )
    tasks.dispatch_alerts(vc.pk)
    after_first = len(mail.outbox)
    tasks.dispatch_alerts(vc.pk)  # re-delivery
    assert len(mail.outbox) == after_first  # no duplicate email


@pytest.mark.django_db
def test_rollup_analytics_counts_outcomes(settings):
    """rollup_analytics returns leak-safe per-outcome counts (no cost/margin)."""
    from voice import tasks
    from voice.models import Outcome, VoiceCall

    VoiceCall.objects.create(call_id="r1", outcome=Outcome.FAQ_ANSWERED)
    VoiceCall.objects.create(call_id="r2", outcome=Outcome.SUGGESTED)
    VoiceCall.objects.create(call_id="r3", outcome=Outcome.SUGGESTED)
    out = tasks.rollup_analytics()
    assert out["calls_total"] == 3
    assert out["by_outcome"]["suggested"] == 2
    assert "cost" not in str(out) and "margin" not in str(out)
