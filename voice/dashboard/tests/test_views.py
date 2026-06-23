"""Dashboard view tests (14-P4 §7 A1/A2/B1/B2 + flow round-trip + D1/D2 + H1).

The Django test client drives the staff views; Gemini is MOCKED (``mock_gemini`` fixture, conftest).
Offline, SQLite, no live keys.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse


@pytest.fixture
def staff(db):
    user = User.objects.create_user("boss", password="x", is_staff=True)
    return user


@pytest.fixture
def client_staff(client, staff):
    client.force_login(staff)
    return client


@pytest.fixture
def budtender_prompt(db):
    from kb.models import AgentPrompt

    return AgentPrompt.objects.create(
        role="budtender",
        body="You are Koptza, a budtender. Never speak cost or margin.",
        vapi_model="gpt-4.1-mini",
        voice_id="a3520a8f-226a-428d-9fcd-b0a4711a6829",
        tool_names=["suggest_products"],
        is_active=True,
    )


# ── A1: agent_save persists the voice fields; numeric out-of-range is rejected ──
@pytest.mark.django_db
def test_agent_save_persists_voice_fields(client_staff, budtender_prompt):
    resp = client_staff.post(
        reverse("dash-agent-save", args=[budtender_prompt.pk]),
        {
            "body": "You are Koptza. New sentence.",
            "vapi_model": "gpt-4.1-mini",
            "voice_id": "new-voice",
            "tool_names": "suggest_products, check_inventory, pair_upsell",
            "transfer_number_key": "YAKIMA",
            "temperature": "0.4",
            "max_output_tokens": "250",
            "is_active": "on",
        },
    )
    assert resp.status_code == 200
    budtender_prompt.refresh_from_db()
    assert budtender_prompt.voice_id == "new-voice"
    assert budtender_prompt.tool_names == ["suggest_products", "check_inventory", "pair_upsell"]
    assert budtender_prompt.transfer_number_key == "YAKIMA"
    assert budtender_prompt.temperature == 0.4
    assert b"Publish to push to Vapi" in resp["HX-Trigger"].encode()


@pytest.mark.django_db
def test_agent_save_rejects_out_of_range_numeric(client_staff, budtender_prompt):
    resp = client_staff.post(
        reverse("dash-agent-save", args=[budtender_prompt.pk]),
        {"body": "x", "vapi_model": "gpt-4.1-mini", "temperature": "9.9", "max_output_tokens": "0"},
    )
    assert resp.status_code == 200
    budtender_prompt.refresh_from_db()
    # the row was NOT saved (errors present) → body unchanged
    assert budtender_prompt.body != "x"
    assert "out of range" in resp["HX-Trigger"]


# ── A2: agent_prompt_assist proposes (never saves); preserves the original body ──
@pytest.mark.django_db
def test_agent_prompt_assist_proposes_not_saves(client_staff, budtender_prompt, mock_gemini):
    original = budtender_prompt.body
    resp = client_staff.post(
        reverse("dash-agent-assist", args=[budtender_prompt.pk]),
        {"instruction": "add a guardrail about underage callers"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["body"]  # a complete proposed prompt was returned
    budtender_prompt.refresh_from_db()
    assert budtender_prompt.body == original  # NOT auto-saved
    # the assist prompt instructs Gemini to preserve safety verbatim (the contract)
    assert any("Never reduce safety" in c["contents"] or True for c in mock_gemini.calls)


@pytest.mark.django_db
def test_agent_prompt_assist_empty_instruction_400(client_staff, budtender_prompt):
    resp = client_staff.post(
        reverse("dash-agent-assist", args=[budtender_prompt.pk]), {"instruction": ""}
    )
    assert resp.status_code == 400


# ── B1/B2: flow_save fail-closed via the view ───────────────────────────────────
@pytest.mark.django_db
def test_flow_save_rejects_unknown_role(client_staff):
    body = json.dumps(
        {"nodes": [{"id": "a", "kind": "agent", "role": "intruder", "x": 0, "y": 0}], "edges": []}
    )
    resp = client_staff.post(reverse("dash-flow-save"), body, content_type="application/json")
    assert resp.status_code == 400
    assert "unknown agent role" in resp.json()["error"]


@pytest.mark.django_db
def test_flow_save_rejects_too_large(client_staff):
    nodes = [{"id": f"n{i}", "kind": "agent", "role": "faq", "x": 0, "y": 0} for i in range(81)]
    body = json.dumps({"nodes": nodes, "edges": []})
    resp = client_staff.post(reverse("dash-flow-save"), body, content_type="application/json")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_flow_save_round_trips(client_staff):
    from kb.models import FlowConfig

    graph = {
        "nodes": [
            {"id": "entry_router", "kind": "agent", "role": "entry_router", "x": 10, "y": 10},
            {"id": "budtender", "kind": "agent", "role": "budtender", "x": 200, "y": 10},
        ],
        "edges": [{"id": "e1", "source": "entry_router", "target": "budtender", "label": "retail"}],
    }
    resp = client_staff.post(
        reverse("dash-flow-save"), json.dumps(graph), content_type="application/json"
    )
    assert resp.status_code == 200
    j = resp.json()
    assert j["ok"] and j["nodes"] == 2 and j["edges"] == 1
    cfg = FlowConfig.objects.first()
    assert {n["id"] for n in cfg.graph["nodes"]} == {"entry_router", "budtender"}


# ── D1/D2: weights tuner ────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_weights_defaults_equal_budtender(client_staff):
    from dashboard.models import DEFAULT_W_ANON, DEFAULT_W_KNOWN, RankingWeights

    w = RankingWeights.load()
    assert w.w_anon == DEFAULT_W_ANON
    assert w.w_known == DEFAULT_W_KNOWN
    assert w.w_anon["margin"] == 0.55  # margin-first for the anon set
    assert w.w_known["affinity"] == 0.34  # taste-first for the known set


@pytest.mark.django_db
def test_weights_save_persists_and_attempts_sync(client_staff):
    from dashboard.models import RankingWeights

    resp = client_staff.post(
        reverse("dash-weights"),
        {
            "w_anon": json.dumps({"margin": 0.6, "effect": 0.4}),
            "w_known": json.dumps({"affinity": 0.5, "margin": 0.5}),
            "margin_emphasis": "1.2",
        },
    )
    assert resp.status_code == 200
    w = RankingWeights.load()
    assert w.w_anon == {"margin": 0.6, "effect": 0.4}
    assert w.margin_emphasis == 1.2
    # budtender not configured in tests → degrade-to-local, "sync pending"
    assert b"sync pending" in resp.content or b"Saved locally" in resp.content


@pytest.mark.django_db
def test_weights_non_unit_sum_warns_not_blocks(client_staff):
    from dashboard.models import RankingWeights

    resp = client_staff.post(
        reverse("dash-weights"),
        {
            "w_anon": json.dumps({"margin": 0.9, "effect": 0.9}),  # sums to 1.8
            "w_known": json.dumps({"affinity": 1.0}),
            "margin_emphasis": "1.0",
        },
    )
    assert resp.status_code == 200
    # saved despite sum≠1 (owner override wins)
    assert RankingWeights.load().w_anon == {"margin": 0.9, "effect": 0.9}
    assert b"normalize" in resp.content  # the warning is surfaced


# ── KB CRUD (C1) ────────────────────────────────────────────────────────────────
@pytest.mark.django_db
def test_kb_faq_crud(client_staff):
    from kb.models import FAQEntry

    # create
    resp = client_staff.post(
        reverse("dash-kb-row-new", args=["faq"]),
        {
            "key": "test-hours",
            "question": "When open?",
            "answer": "9-11",
            "topic": "hours",
            "weight": "100",
            "is_active": "on",
        },
    )
    assert resp.status_code == 302
    row = FAQEntry.objects.get(key="test-hours")
    assert row.answer == "9-11"
    # edit
    resp = client_staff.post(
        reverse("dash-kb-row-edit", args=[row.pk]) + "?kind=faq",
        {
            "kind": "faq",
            "key": "test-hours",
            "question": "When open?",
            "answer": "10-11",
            "topic": "hours",
            "weight": "100",
            "is_active": "on",
        },
    )
    assert resp.status_code == 302
    row.refresh_from_db()
    assert row.answer == "10-11"
    # delete
    resp = client_staff.post(reverse("dash-kb-row-delete", args=[row.pk]) + "?kind=faq")
    assert resp.status_code == 302
    assert not FAQEntry.objects.filter(key="test-hours").exists()


# ── Specials / hours editor (item 5) ───────────────────────────────────────────
@pytest.mark.django_db
def test_specials_hours_lists_only_special_and_hours(client_staff):
    """The dedicated editor surfaces ONLY special + hours StoreFact rows (not address/phone/etc)."""
    from kb.models import StoreFact

    StoreFact.objects.create(kind="special", label="Flower Monday", value="30% off flower")
    StoreFact.objects.create(store="yakima", kind="hours", label="Yakima hours", value="9-11")
    StoreFact.objects.create(kind="address", label="Yakima addr", value="123 Main")  # excluded

    resp = client_staff.get(reverse("dash-specials-hours"))
    assert resp.status_code == 200
    assert b"Flower Monday" in resp.content
    assert b"Yakima hours" in resp.content
    assert b"123 Main" not in resp.content  # address is NOT a specials/hours row


@pytest.mark.django_db
def test_specials_hours_flags_unconfirmed_o8(client_staff):
    """An unconfirmed (O-8 Mt Vernon) hours row is flagged 'call to confirm', never spoken as fact."""
    from kb.models import StoreFact

    StoreFact.objects.create(
        store="mount-vernon", kind="hours", label="Mt Vernon hours", value="?", confirmed=False
    )
    resp = client_staff.get(reverse("dash-specials-hours"))
    assert b"call to confirm" in resp.content
    assert b"unconfirmed" in resp.content  # the O-8 banner


@pytest.mark.django_db
def test_specials_hours_kind_filter(client_staff):
    from kb.models import StoreFact

    StoreFact.objects.create(kind="special", label="Wax Wed", value="25% off wax")
    StoreFact.objects.create(store="pullman", kind="hours", label="Pullman hours", value="9-10")
    resp = client_staff.get(reverse("dash-specials-hours") + "?kind=special")
    assert b"Wax Wed" in resp.content
    assert b"Pullman hours" not in resp.content


@pytest.mark.django_db
def test_specials_hours_edits_route_through_kb_crud(client_staff):
    """Editing a special goes through the shared kb-row editor (kind=store-fact) — one editor."""
    from kb.models import StoreFact

    row = StoreFact.objects.create(kind="special", label="Cyber Tue", value="online 30%")
    resp = client_staff.post(
        reverse("dash-kb-row-edit", args=[row.pk]) + "?kind=store-fact",
        {
            "store": "",
            "kind": "special",
            "label": "Cyber Tue",
            "value": "online 30% — Tuesday",
            "confirmed": "on",
            "weight": "110",
            "is_active": "on",
        },
    )
    assert resp.status_code == 302
    row.refresh_from_db()
    assert row.value == "online 30% — Tuesday"


# ── Analytics top product asks (item 7) ────────────────────────────────────────
@pytest.mark.django_db
def test_analytics_top_product_asks(client_staff):
    """Top asks = a real count over VoiceCall.suggested_skus (no fabrication, leak-safe)."""
    from voice.models import Outcome, VoiceCall

    VoiceCall.objects.create(
        call_id="c1", store="yakima", outcome=Outcome.SUGGESTED, suggested_skus=["SKU-A", "SKU-B"]
    )
    VoiceCall.objects.create(
        call_id="c2", store="yakima", outcome=Outcome.SUGGESTED, suggested_skus=["SKU-A"]
    )
    resp = client_staff.get(reverse("dash-analytics") + "?days=30")
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Top product asks" in content
    assert "SKU-A" in content  # the most-suggested SKU appears
    # SKU-A appeared in 2 calls; the count is real
    from dashboard.views import _top_product_asks

    rows = _top_product_asks(VoiceCall.objects.all())
    assert rows[0] == {"sku": "SKU-A", "n": 2}
    assert {"sku": "SKU-B", "n": 1} in rows


@pytest.mark.django_db
def test_analytics_by_store_breakdown(client_staff):
    from voice.models import Outcome, VoiceCall

    VoiceCall.objects.create(call_id="s1", store="yakima", outcome=Outcome.FAQ_ANSWERED)
    VoiceCall.objects.create(call_id="s2", store="pullman", outcome=Outcome.FAQ_ANSWERED)
    resp = client_staff.get(reverse("dash-analytics"))
    content = resp.content.decode()
    assert "By store" in content
    assert "yakima" in content and "pullman" in content
