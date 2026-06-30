"""Staff-gate sweep (14-P4 §7 H1) — every ``dash-*`` route is ``@staff_member_required``.

An anonymous GET to each route redirects (302) to the admin login. Parametrized over the explicit
``dash-*`` route table; a missing route here is caught by ``test_sweep_covers_all_dash_routes``
(which diffs against the live URLConf). Offline, SQLite.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

# (route name, reverse kwargs) — every named dash-* route, with a placeholder per captured arg.
DASH_ROUTES = [
    ("dash-overview", {}),
    ("dash-analytics", {}),
    ("dash-agents", {}),
    ("dash-agent-save", {"pk": 1}),
    ("dash-agent-assist", {"pk": 1}),
    ("dash-agent-publish", {"pk": 1}),
    ("dash-agent-detail", {"role": "budtender"}),
    ("dash-flow", {}),
    ("dash-flow-save", {}),
    ("dash-kb", {}),
    ("dash-kb-reindex", {}),
    ("dash-kb-source", {"kind": "faq"}),
    ("dash-kb-row-new", {"kind": "faq"}),
    ("dash-kb-row-edit", {"pk": 1}),
    ("dash-kb-row-delete", {"pk": 1}),
    ("dash-specials-hours", {}),
    ("dash-weights", {}),
    ("dash-credentials", {}),
    ("dash-credentials-save", {}),
    ("dash-calls", {}),
    ("dash-conversation-history", {}),
    ("dash-call-log", {}),
    ("dash-chat-history", {}),
    ("dash-chat-detail", {}),
    ("dash-call-detail", {"pk": 1}),
    ("dash-call-transcript", {"pk": 1}),
    ("dash-call-fetch-full", {"pk": 1}),
    ("dash-escalations", {}),
    ("dash-vendor-queue", {}),
    ("dash-vendor-update", {"pk": 1}),
    ("dash-publish", {}),
    ("dash-publish-run", {}),
]


@pytest.mark.django_db
@pytest.mark.parametrize("name,kwargs", DASH_ROUTES, ids=[r[0] for r in DASH_ROUTES])
def test_anonymous_is_redirected_to_login(client, name, kwargs):
    url = reverse(name, kwargs=kwargs)
    # GET every route; @require_POST views still gate auth FIRST → a redirect, never a 200/405-as-200.
    resp = client.get(url)
    assert resp.status_code in (301, 302), f"{name} did not gate anonymous access"
    assert "/admin/login" in resp["Location"] or "next=" in resp["Location"]


def test_sweep_covers_all_dash_routes():
    """The sweep table must cover EVERY dash-* route in the live URLConf (no gap)."""
    from dashboard import urls as dash_urls

    live = {p.name for p in dash_urls.urlpatterns if p.name and p.name.startswith("dash-")}
    covered = {r[0] for r in DASH_ROUTES}
    assert live == covered, f"uncovered routes: {live - covered}; stale: {covered - live}"
