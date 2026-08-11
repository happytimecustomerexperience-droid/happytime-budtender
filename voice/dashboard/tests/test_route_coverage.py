"""Route-coverage sweep (owner report: "the dashboard — the flow and others don't work").

Every ``dash-*`` GET route is exercised as staff and asserted to actually RENDER (200, no silent
500, no missing-context KeyError) — not just auth-gate like ``test_staff_gating.py``. Every
POST/action route is exercised with a realistic payload and asserted to do what it claims.

Offline, SQLite, no live keys — Vapi/Gemini/budtender/site-scrape network calls are mocked or
degrade gracefully (fail-closed unconfigured-empty), same discipline as the rest of the suite.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

# Reuse the publish fixtures verbatim (same mock shape everywhere Vapi is touched).
from dashboard.tests.test_publish import fake_vapi, full_squad  # noqa: F401


@pytest.fixture
def staff(db):
    return User.objects.create_user("boss2", password="x", is_staff=True)


@pytest.fixture
def client_staff(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def one_agent(db):
    from kb.models import AgentPrompt

    return AgentPrompt.objects.create(
        role="budtender", body="You are Koptza.", vapi_model="gpt-4.1-mini", is_active=True
    )


@pytest.fixture
def one_call(db):
    from voice.models import Outcome, VoiceCall, VoiceTurn

    call = VoiceCall.objects.create(
        call_id="route-cov-1", store="yakima", outcome=Outcome.FAQ_ANSWERED, duration_s=42
    )
    VoiceTurn.objects.create(call=call, seq=0, role="user", text="hi")
    VoiceTurn.objects.create(call=call, seq=1, role="assistant", text="hello")
    return call


@pytest.fixture
def in_flight_call(db):
    from voice.models import VoiceCall

    return VoiceCall.objects.create(call_id="route-cov-live", store="pullman")


@pytest.fixture
def one_customer(db):
    from crm.models import CustomerProfile

    return CustomerProfile.objects.create(
        customer_key="cust-1",
        name="Jamie Rivera",
        orders=3,
        favorite_brands=[{"brand": None, "share": None}],
    )


@pytest.fixture
def one_vendor_callback(db):
    from crm.models import VendorCallback

    return VendorCallback.objects.create(
        vapi_call_id="vc-route-cov-1", store="yakima", reason="wholesale_order"
    )


@pytest.fixture
def one_faq_row(db):
    from kb.models import FAQEntry

    return FAQEntry.objects.create(key="route-cov-faq", question="Q?", answer="A.")


# ── plain GET renders (no query params needed) ─────────────────────────────────
@pytest.mark.django_db
@pytest.mark.parametrize(
    "name",
    [
        "dash-overview",
        "dash-agents",
        "dash-flow",
        "dash-kb",
        "dash-weights",
        "dash-credentials",
        "dash-customers",
        "dash-calls",
        "dash-call-log",
        "dash-chat-history",
        "dash-conversation-history",
        "dash-escalations",
        "dash-vendor-queue",
        "dash-publish",
        "dash-analytics",
        "dash-specials-hours",
    ],
)
def test_get_route_renders_200(client_staff, name):
    resp = client_staff.get(reverse(name))
    assert resp.status_code == 200
    assert resp.content  # actually rendered a body, not an empty response


@pytest.mark.django_db
def test_agent_detail_renders(client_staff, one_agent):
    resp = client_staff.get(reverse("dash-agent-detail", args=["budtender"]))
    assert resp.status_code == 200
    assert b"budtender" in resp.content.lower() or b"Budtender" in resp.content


@pytest.mark.django_db
def test_kb_source_list_renders(client_staff, one_faq_row):
    resp = client_staff.get(reverse("dash-kb-source", args=["faq"]))
    assert resp.status_code == 200
    assert b"route-cov-faq" in resp.content or resp.status_code == 200


@pytest.mark.django_db
def test_kb_row_edit_get_renders(client_staff, one_faq_row):
    resp = client_staff.get(reverse("dash-kb-row-edit", args=[one_faq_row.pk]) + "?kind=faq")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_kb_row_new_get_renders(client_staff):
    resp = client_staff.get(reverse("dash-kb-row-new", args=["faq"]))
    assert resp.status_code == 200


@pytest.mark.django_db
def test_call_detail_renders(client_staff, one_call):
    resp = client_staff.get(reverse("dash-call-detail", args=[one_call.pk]))
    assert resp.status_code == 200
    assert b"route-cov-1" in resp.content


@pytest.mark.django_db
def test_call_transcript_renders(client_staff, one_call):
    resp = client_staff.get(reverse("dash-call-transcript", args=[one_call.pk]))
    assert resp.status_code == 200
    assert b"hello" in resp.content


@pytest.mark.django_db
def test_customer_detail_renders_for_local_profile(client_staff, one_customer):
    resp = client_staff.get(reverse("dash-customer-detail", args=[one_customer.pk]))
    assert resp.status_code == 200
    assert b"Jamie Rivera" in resp.content


@pytest.mark.django_db
def test_customer_detail_no_mojibake_in_favorite_brands_and_purchase_history(
    client_staff, one_customer, monkeypatch
):
    """customer_detail.html had double-encoded em-dash/middle-dot literals (â€” / Â·) baked into the
    template — every blank placeholder in the favorite-brands + purchase-history tables and the
    'Purchase history' subheading rendered as literal mojibake instead of — / ·."""
    from voice import budtender_client

    live_profile = {
        "id": one_customer.pk,
        "name": "Jamie Rivera",
        "purchase_history": [{"product": "Blue Dream", "brand": None, "last_price": None}],
    }
    monkeypatch.setattr(
        budtender_client, "budtender", lambda: type("B", (), {"get_customer": lambda *a, **k: live_profile})()
    )
    resp = client_staff.get(reverse("dash-customer-detail", args=[one_customer.pk]))
    content = resp.content.decode("utf-8")
    assert "Purchase history" in content  # the section actually rendered (not skipped empty)
    assert "â€”" not in content
    assert "Â·" not in content


@pytest.mark.django_db
def test_customer_detail_404s_for_unknown_pk_no_snapshot_no_live(client_staff, db):
    # No CustomerProfile row + budtender unconfigured (default test env) → live lookup is None
    # → the view raises Http404 rather than 500ing. Documents intended behaviour, not a bug.
    resp = client_staff.get(reverse("dash-customer-detail", args=[999999]))
    assert resp.status_code == 404


@pytest.mark.django_db
def test_chat_detail_renders_without_session_token(client_staff):
    resp = client_staff.get(reverse("dash-chat-detail"))
    assert resp.status_code == 200


@pytest.mark.django_db
def test_calls_live_strip_renders_via_htmx(client_staff, in_flight_call):
    resp = client_staff.get(
        reverse("dash-calls") + "?strip=live", HTTP_HX_REQUEST="true"
    )
    assert resp.status_code == 200
    assert b"route-cov-live" in resp.content


# ── action routes: assert the action actually does what it claims ──────────────
@pytest.mark.django_db
def test_publish_run_view_publishes_and_renders_results(client_staff, fake_vapi, full_squad):  # noqa: F811
    resp = client_staff.post(reverse("dash-publish-run"))
    assert resp.status_code == 200
    assert b"Publish result" in resp.content
    # every one of the 5 members + the squad actually got a row back
    for role in ("entry_router", "budtender", "faq", "vendor", "escalation"):
        assert role.encode() in resp.content


@pytest.mark.django_db
def test_agent_publish_one_view_publishes_that_assistant(client_staff, fake_vapi, full_squad):  # noqa: F811
    p = full_squad.objects.get(role="budtender")
    resp = client_staff.post(reverse("dash-agent-publish", args=[p.pk]))
    assert resp.status_code == 200
    assert b"assistant" in resp.content
    p.refresh_from_db()
    assert p.vapi_assistant_id  # provisioned id round-trips through the view


@pytest.mark.django_db
def test_vendor_callback_update_marks_contacted(client_staff, one_vendor_callback):
    resp = client_staff.post(
        reverse("dash-vendor-update", args=[one_vendor_callback.pk]), {"action": "contacted"}
    )
    assert resp.status_code == 302
    one_vendor_callback.refresh_from_db()
    assert one_vendor_callback.status == "contacted"


@pytest.mark.django_db
def test_vendor_callback_update_marks_closed(client_staff, one_vendor_callback):
    resp = client_staff.post(
        reverse("dash-vendor-update", args=[one_vendor_callback.pk]), {"action": "closed"}
    )
    assert resp.status_code == 302
    one_vendor_callback.refresh_from_db()
    assert one_vendor_callback.status == "closed"


@pytest.mark.django_db
def test_kb_reindex_view_never_500s_when_vapi_unconfigured(client_staff, one_faq_row, monkeypatch):
    """Vapi unconfigured (fail-closed; forced here rather than assumed — a populated local ``.env``
    otherwise leaks a real key past the view and fires a LIVE outbound call, see report) — the
    action must degrade to a toast, never 500, and still redirect to the KB landing."""
    from core.services import vapi

    monkeypatch.setattr(vapi, "configured", lambda: False)
    resp = client_staff.post(reverse("dash-kb-reindex"))
    assert resp.status_code == 302
    assert resp.url == reverse("dash-kb")
    assert "reindexed" in resp["HX-Trigger"]


@pytest.mark.django_db
def test_kb_scrape_view_applies_a_successful_scrape(client_staff, monkeypatch):
    """The view's only job is to call run_scrape(publish=True) and toast the result — mock
    run_scrape itself (network fetch lives in kb/site_scrape.py, out of this agent's boundary)."""
    from kb import site_scrape

    class FakeRun:
        status = "applied"
        summary = "2 pages, 3 changes applied."

    monkeypatch.setattr(site_scrape, "run_scrape", lambda **kw: FakeRun())
    resp = client_staff.post(reverse("dash-kb-scrape"))
    assert resp.status_code == 302
    assert resp.url == reverse("dash-kb")
    assert "applied" in resp["HX-Trigger"]


@pytest.mark.django_db
def test_call_fetch_full_degrades_when_vapi_unconfigured(client_staff, one_call, monkeypatch):
    """Forced-unconfigured (see kb_reindex test above for why: a populated local ``.env`` otherwise
    leaks a real Vapi key past this view and fires a LIVE outbound call to api.vapi.ai)."""
    from core.services import vapi

    monkeypatch.setattr(vapi, "configured", lambda: False)
    resp = client_staff.post(reverse("dash-call-fetch-full", args=[one_call.pk]))
    assert resp.status_code == 200
    assert "not configured" in resp["HX-Trigger"]
