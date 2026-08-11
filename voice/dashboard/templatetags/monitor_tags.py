"""Template filter over ``dashboard.monitor.call_outcome_badge``.

Django templates can't call a function with an argument, so the outcome→(label, colour) mapping in
``monitor.py`` needs a filter wrapper to be usable in a template: ``{{ call.outcome|outcome_badge }}``.
No logic lives here — this only adapts ``call_outcome_badge``'s return shape for template use.
"""

from __future__ import annotations

from django import template

from ..monitor import call_outcome_badge

register = template.Library()


@register.filter(name="outcome_badge")
def outcome_badge(outcome: str) -> dict[str, str]:
    """{{ call.outcome|outcome_badge }} → {"label": ..., "color": ...} for ``<span class="badge {{ b.color }}">{{ b.label }}</span>``."""
    label, color = call_outcome_badge(outcome)
    return {"label": label, "color": color}
