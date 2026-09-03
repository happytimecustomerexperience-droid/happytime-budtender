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

from celery import chain, shared_task
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
            chain(summarize_call.si(voice_call_id), dispatch_alerts.si(voice_call_id)).delay()
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


# ── root-notify nudge (P6 instant-refresh chain; kb/signals.py) ──────────────────
@shared_task(name="voice.notify_budtender", ignore_result=True)
def notify_budtender(kind: str) -> bool:
    """Run the actual (gated, fail-closed) POST — see ``voice.budtender_client._notify``."""
    from voice import budtender_client

    if kind == "persona":
        return budtender_client.notify_persona_refresh()
    return budtender_client.notify_store_facts_refresh()


def dispatch_budtender_notify(kind: str) -> None:
    """Queue the root-notify POST when Celery is enabled, else run it inline — same
    queue-or-inline shape as ``run_post_call``. The notify itself is a no-op when
    ``HHT_NOTIFY_BUDTENDER`` is off or unconfigured, so this is cheap to call unconditionally."""
    if _use_celery():
        try:
            notify_budtender.delay(kind)
            return
        except Exception:  # noqa: BLE001 — broker down → inline, never drop the notification
            logger.warning(
                "celery enqueue failed for notify_budtender(%s); running inline", kind, exc_info=True
            )
    notify_budtender(kind)


# ── nightly store-facts vs. public-site drift check (P6) ─────────────────────────
@shared_task(name="voice.check_store_facts_nightly", ignore_result=True)
def check_store_facts_nightly() -> dict:
    """Compare ``kb.StoreFact`` against the public site's ``/api/refresh-constants`` and send one
    staff alert (existing ``crm.sinks`` email/n8n path) on drift. Read-only; never raises — a
    request failure just skips the comparison for this run (there is always a next night)."""
    from kb.management.commands.check_store_facts import diff_against_site

    try:
        rows = diff_against_site()
    except Exception:  # noqa: BLE001 — a fetch/compare failure must never crash the beat worker
        logger.warning("check_store_facts_nightly: comparison failed", exc_info=True)
        return {}
    mismatches = [r for r in rows if r["status"] == "MISMATCH"]
    if not mismatches:
        return {"drift": False, "mismatches": 0}
    from crm import sinks

    sinks.send_staff_alert(
        subject="[Happy Time voice] store-facts drift vs. public site",
        markdown_table=_rows_to_markdown_table(rows),
    )
    return {"drift": True, "mismatches": len(mismatches)}


def _rows_to_markdown_table(rows: list[dict]) -> str:
    headers = ["store", "fact", "storefact", "site", "status"]
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(r[h]) for h in headers) + " |")
    return "\n".join(lines)
