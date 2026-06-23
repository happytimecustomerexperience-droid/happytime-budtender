"""Post-call background tasks (P5, gated; 15-P5 §3.5; ADR-021).

The eocr handler writes the durable ``VoiceCall`` row SYNCHRONOUSLY (ADR-017 — never lost), then
hands the NON-critical post-call work to here: the Gemini call summary, the staff-email/Slack
dispatch, and the analytics roll-up. Each is an idempotent ``@shared_task`` keyed on the
``voice_call_id`` so a re-run (Vapi retry / worker restart) never duplicates an email or a summary.

Gating + sync fallback (binding, 15-P5 §3.5 / §6 AC-5):
  * ``HHT_USE_CELERY=1`` → ``run_post_call`` enqueues the tasks on the queue (``.delay``) so the
    webhook returns fast and a slow Gemini/SMTP call never stalls the Vapi callback.
  * ``HHT_USE_CELERY=0`` (default, P2 behavior) OR no broker reachable → the tasks run INLINE,
    exactly as P2 did, so the suite runs broker-free and a missing Redis degrades to inline (never
    drops the work).
The durable ``VoiceCall`` write is NOT here — it stays synchronous in the webhook.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.conf import settings

logger = logging.getLogger(__name__)


def _use_celery() -> bool:
    """Whether the queue is enabled (``HHT_USE_CELERY``). Off → inline P2 path."""
    return bool(getattr(settings, "HHT_USE_CELERY", False))


# ── the three idempotent tasks ──────────────────────────────────────────────────
@shared_task(name="voice.summarize_call", ignore_result=True)
def summarize_call(voice_call_id: int) -> str:
    """Gemini call summary → write back onto the ``VoiceCall`` row. Idempotent: skips if a summary
    already exists (a re-run is a no-op). Degrade-safe — a Gemini failure leaves the row untouched
    (the durable record is already safe)."""
    from voice import summarize
    from voice.models import VoiceCall

    vc = VoiceCall.objects.filter(pk=voice_call_id).first()
    if vc is None:
        logger.warning("summarize_call: VoiceCall %s not found", voice_call_id)
        return ""
    if vc.ai_summary:  # idempotent — already summarized
        return vc.ai_summary
    summary = summarize.summarize_call(vc)
    if summary:
        vc.ai_summary = summary
        vc.save(update_fields=["ai_summary", "updated_at"])
    return summary


@shared_task(name="voice.dispatch_alerts", ignore_result=True)
def dispatch_alerts(voice_call_id: int) -> dict:
    """Fire the per-call staff alert (email + optional Slack) via ``crm.sinks.dispatch`` — already
    idempotent per ``(voice_call, sink)`` (the AlertDelivery ledger), so a re-run never re-sends.
    Never raises (a sink failure is recorded, not fatal)."""
    from crm import sinks
    from voice.models import VoiceCall

    vc = VoiceCall.objects.filter(pk=voice_call_id).first()
    if vc is None:
        logger.warning("dispatch_alerts: VoiceCall %s not found", voice_call_id)
        return {}
    try:
        return sinks.dispatch(vc)
    except Exception:  # noqa: BLE001 — alerting must never crash the worker
        logger.warning("dispatch_alerts failed for %s", voice_call_id, exc_info=True)
        return {}


@shared_task(name="voice.rollup_analytics", ignore_result=True)
def rollup_analytics(date_iso: str | None = None) -> dict:
    """A light per-period aggregate over the durable ``VoiceCall`` rows (the analytics summary feed).
    Leak-safe: counts only, no cost/margin. Best-effort; returns the per-outcome counts for the day
    (or all-time when no date is given). Pure read — never mutates the call log."""
    from django.db.models import Count

    from voice.models import VoiceCall

    qs = VoiceCall.objects.all()
    if date_iso:
        try:
            from datetime import date as _date

            day = _date.fromisoformat(date_iso)
            qs = qs.filter(created_at__date=day)
        except (TypeError, ValueError):
            logger.warning("rollup_analytics: bad date %s; rolling up all", date_iso)
    by_outcome = dict(
        qs.exclude(outcome="").values_list("outcome").annotate(n=Count("id")).order_by()
    )
    return {"date": date_iso, "calls_total": qs.count(), "by_outcome": by_outcome}


# ── the gated dispatcher (queue when enabled, inline otherwise) ──────────────────
def run_post_call(voice_call_id: int) -> None:
    """Run the post-call work for one call — on the queue when ``HHT_USE_CELERY`` is on, else INLINE.

    Enqueue order is independent (summary + alerts); the durable ``VoiceCall`` row already exists
    (the webhook wrote it synchronously). When the broker is unreachable even with the flag on, fall
    back to inline so the work is never silently dropped (15-P5 §6 AC-5 — record + work survive a
    broker outage)."""
    if _use_celery():
        try:
            summarize_call.delay(voice_call_id)
            dispatch_alerts.delay(voice_call_id)
            return
        except Exception:  # noqa: BLE001 — broker down → degrade to inline, never drop the work
            logger.warning(
                "celery enqueue failed for %s; running post-call work inline",
                voice_call_id,
                exc_info=True,
            )
    # Inline path (P2 behavior / sync fallback): call the task bodies directly (NOT via the queue).
    summarize_call(voice_call_id)
    dispatch_alerts(voice_call_id)
