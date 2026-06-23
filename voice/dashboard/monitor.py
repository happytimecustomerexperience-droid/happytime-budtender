"""Call-monitor query helpers (14-P4 §3.1, monitor.py).

Thin read helpers over ``voice.models.VoiceCall`` for the live monitor + call log. No business
logic, no Vapi — just the in-flight-vs-recent split and the outcome badge mapping. Leak-safe by
construction: ``VoiceCall`` carries no product cost/margin field.
"""

from __future__ import annotations

from voice.models import VoiceCall

# in-flight = a call we logged but whose end-of-call-report (which stamps an outcome) hasn't landed.
_IN_FLIGHT_Q = {"outcome": ""}

# outcome → (label, badge-color-key) for the UI; neutral fallback for an unknown/blank outcome.
_OUTCOME_BADGE = {
    "faq_answered": ("FAQ answered", "green"),
    "suggested": ("Suggested", "blue"),
    "escalation": ("Escalation", "red"),
    "vendor_callback": ("Vendor callback", "amber"),
    "abandoned": ("Abandoned", "slate"),
    "error": ("Error", "red"),
}


def call_outcome_badge(outcome: str) -> tuple[str, str]:
    """(label, color-key) for a VoiceCall.outcome — neutral when blank/in-flight."""
    return _OUTCOME_BADGE.get(outcome or "", ("In progress" if not outcome else outcome, "slate"))


def live_calls(limit: int = 25):
    """In-flight calls — logged but no outcome stamped yet (the eocr hasn't classified them)."""
    return VoiceCall.objects.filter(**_IN_FLIGHT_Q).order_by("-created_at")[:limit]


def recent_calls(limit: int = 25):
    """The most-recent calls with an outcome (the live monitor's "recent" strip)."""
    return VoiceCall.objects.exclude(outcome="").order_by("-created_at")[:limit]
