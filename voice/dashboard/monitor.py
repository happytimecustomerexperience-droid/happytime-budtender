"""Call-monitor query helpers (14-P4 §3.1, monitor.py).

Thin read helpers over ``voice.models.VoiceCall`` for the live monitor + call log. No business
logic, no Vapi — just the in-flight-vs-recent split and the outcome badge mapping. Leak-safe by
construction: ``VoiceCall`` carries no product cost/margin field.
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from voice.models import VoiceCall

# in-flight = a call we logged in the last 2 hours whose end-of-call-report (which stamps an
# outcome) hasn't landed. Older blank-outcome rows lost their report — they are not live.
_LIVE_WINDOW = timedelta(hours=2)

# outcome → (label, badge-color-key) for the UI; neutral fallback for an unknown/blank outcome.
_OUTCOME_BADGE = {
    "faq_answered": ("FAQ answered", "green"),
    "suggested": ("Suggested", "blue"),
    "escalation": ("Escalation", "red"),
    "vendor_callback": ("Vendor callback", "amber"),
    "vendor_direct": ("Vendor sent to owner", "blue"),
    "transfer_unavailable": ("Transfer: person unavailable", "amber"),
    "abandoned": ("Abandoned", "slate"),
    "error": ("Error", "red"),
}


def call_outcome_badge(outcome: str) -> tuple[str, str]:
    """(label, color-key) for a VoiceCall.outcome — neutral when blank/in-flight."""
    return _OUTCOME_BADGE.get(outcome or "", ("In progress" if not outcome else outcome, "slate"))


def is_live(call) -> bool:
    """THE live predicate: no outcome yet AND first logged within the last 2 hours. A blank-outcome
    call older than that never got its end-of-call report — it is over, not in flight."""
    return not call.outcome and call.created_at >= timezone.now() - _LIVE_WINDOW


def call_status_badge(call) -> tuple[str, str]:
    """(label, color-key) for a call row: the outcome badge, except a blank-outcome call that is no
    longer live reads "Ended (no report)" rather than "In progress" forever."""
    if not call.outcome and not is_live(call):
        return "Ended (no report)", "amber"
    return call_outcome_badge(call.outcome)


def live_calls(limit: int = 25):
    """In-flight calls — ``is_live`` as a queryset filter (keep the two in step)."""
    since = timezone.now() - _LIVE_WINDOW
    return VoiceCall.objects.filter(outcome="", created_at__gte=since).order_by("-created_at")[:limit]


def recent_calls(limit: int = 25):
    """The most-recent calls with an outcome (the live monitor's "recent" strip)."""
    return VoiceCall.objects.exclude(outcome="").order_by("-created_at")[:limit]
