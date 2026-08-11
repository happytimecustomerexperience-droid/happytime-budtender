"""Outcome-badge colour-coding (dashboard audit fix).

``monitor.call_outcome_badge`` maps a call outcome to a (label, colour-key) pair, but no template
ever invoked it — every badge rendered hardcoded ``class="badge slate"`` regardless of outcome. The
``outcome_badge`` template filter (``dashboard/templatetags/monitor_tags.py``) wraps the function so
templates can do ``{{ call.outcome|outcome_badge }}``. These tests are the ones that would have
caught the original bug: they assert a DISTINCT, non-grey badge colour actually renders for a
coloured outcome on every page that shows one.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.template import Context, Template
from django.urls import reverse

from dashboard import monitor
from dashboard.templatetags.monitor_tags import outcome_badge
from voice.models import Outcome, VoiceCall


@pytest.fixture
def staff(db):
    return User.objects.create_user("boss", password="x", is_staff=True)


@pytest.fixture
def client_staff(client, staff):
    client.force_login(staff)
    return client


# ── unit: the filter for every outcome monitor.call_outcome_badge knows about ──
@pytest.mark.parametrize("outcome", list(monitor._OUTCOME_BADGE.keys()))
def test_outcome_badge_filter_matches_monitor_for_every_known_outcome(outcome):
    label, color = monitor.call_outcome_badge(outcome)
    badge = outcome_badge(outcome)
    assert badge["label"] == label
    assert badge["color"] == color


@pytest.mark.parametrize("outcome", ["", "totally-unknown-outcome", None])
def test_outcome_badge_filter_blank_or_unknown_falls_back_to_slate(outcome):
    badge = outcome_badge(outcome)
    assert badge["color"] == "slate"  # neutral fallback, never crashes
    assert badge["label"]  # always has SOME label


def test_outcome_badge_filter_usable_from_a_template():
    """The exact usage the templates rely on: {% with b=o|outcome_badge %}...{{ b.color }}..."""
    tpl = Template(
        '{% load monitor_tags %}{% with b=o|outcome_badge %}'
        '<span class="badge {{ b.color }}">{{ b.label }}</span>{% endwith %}'
    )
    rendered = tpl.render(Context({"o": "escalation"}))
    assert '<span class="badge red">Escalation</span>' == rendered


# ── render: a coloured outcome must render its DISTINCT badge colour, not grey ─
@pytest.mark.django_db
def test_calls_page_recent_shows_coloured_badge_not_grey(client_staff):
    VoiceCall.objects.create(call_id="c-esc", store="yakima", outcome=Outcome.ESCALATION)
    resp = client_staff.get(reverse("dash-calls"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert 'class="badge red"' in content  # escalation → red, not slate
    assert '<span class="badge slate">Escalation</span>' not in content


@pytest.mark.django_db
def test_call_log_shows_coloured_badge_not_grey(client_staff):
    VoiceCall.objects.create(call_id="c-sug", store="yakima", outcome=Outcome.SUGGESTED)
    resp = client_staff.get(reverse("dash-call-log"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert 'class="badge blue"' in content  # suggested → blue, not slate
    assert '<span class="badge slate">Suggested</span>' not in content


@pytest.mark.django_db
def test_call_detail_shows_coloured_badge_not_grey(client_staff):
    call = VoiceCall.objects.create(
        call_id="c-vc", store="yakima", outcome=Outcome.VENDOR_CALLBACK
    )
    resp = client_staff.get(reverse("dash-call-detail", args=[call.pk]))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert 'class="badge amber"' in content  # vendor_callback → amber, not slate
    assert '<span class="badge slate">Vendor callback</span>' not in content


@pytest.mark.django_db
def test_calls_live_strip_renders_without_crashing(client_staff):
    """In-flight calls always have a blank outcome (slate/"In progress") — the strip must still
    wire the filter cleanly rather than never touching it."""
    VoiceCall.objects.create(call_id="c-live", store="yakima", outcome="")
    resp = client_staff.get(
        reverse("dash-calls") + "?strip=live", HTTP_HX_REQUEST="true"
    )
    assert resp.status_code == 200
    assert b"c-live" in resp.content
