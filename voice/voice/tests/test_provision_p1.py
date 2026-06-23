"""tests/test_provision_p1.py — P1 provisioning: budtender member + tools + squad edge (11-P1 §5).

The Vapi HTTP layer is MOCKED (reuses the FakeAccount from test_provision). Asserts (AC G):
G1. provision creates the 3 suggestion tools + the budtender + entry_router assistants + attaches
    their toolIds; G2. a re-run is zero-drift (0 creates); G3. voice/model/keyterms appear ONCE on
    the budtender assistant payload; plus the entry_router →(retail)→ budtender squad edge resolves.
Offline, no live keys.
"""

from __future__ import annotations

import json

import pytest

from core.services import vapi
from voice import provision
from voice.tests.test_provision import FakeAccount


@pytest.fixture
def fake_vapi(monkeypatch):
    acct = FakeAccount()
    monkeypatch.setattr(vapi, "configured", lambda: True)
    monkeypatch.setattr(vapi, "auth_ok", lambda: {"ok": True, "configured": True, "error": ""})
    for name in (
        "find_tool_by_name",
        "get_tool",
        "create_tool",
        "patch_tool",
        "find_assistant_by_name",
        "get_assistant",
        "create_assistant",
        "patch_assistant",
        "find_squad_by_name",
        "get_squad",
        "create_squad",
        "patch_squad",
    ):
        monkeypatch.setattr(vapi, name, getattr(acct, name))
    monkeypatch.setattr(vapi, "find_phone_number", lambda _x: None)
    from kb import vapi_files

    monkeypatch.setattr(vapi_files, "mirror_all", lambda: {"skipped": "not configured"})
    return acct


@pytest.fixture
def p1_prompts(db):
    """Seed the faq + entry_router + budtender AgentPrompt rows (the members P1 provisions)."""
    from kb.models import AgentPrompt

    AgentPrompt.objects.create(role="faq", body="You are Koptza (faq).", is_active=True)
    AgentPrompt.objects.create(
        role="entry_router", body="You are Koptza. Confirm 21+. Classify.", is_active=True
    )
    AgentPrompt.objects.create(
        role="budtender",
        body="You are Koptza, a budtender. Slot-fill. Speak why_this and out-the-door prices.",
        is_active=True,
    )


# ── G1. budtender member + 3 tools provisioned ──────────────────────────────────
@pytest.mark.django_db
def test_provision_creates_budtender_member_and_tools(fake_vapi, p1_prompts):
    report = provision.provision_all(dry_run=False)
    assert report.ok
    names = {(r.kind, r.name): r for r in report.results}
    for tool in ("suggest_products", "check_inventory", "pair_upsell"):
        assert names[("tool", tool)].action == "created"
        assert names[("tool", tool)].vapi_id
    assert names[("assistant", "budtender")].action == "created"
    assert names[("assistant", "entry_router")].action == "created"


@pytest.mark.django_db
def test_budtender_assistant_attaches_three_tool_ids(fake_vapi, p1_prompts):
    # tools first so the assistant resolves their ids.
    for t in ("suggest_products", "check_inventory", "pair_upsell"):
        provision.ensure_tool(t)
    payload, warnings = provision.build_assistant_payload("budtender", name="budtender")
    assert not [w for w in warnings if w.startswith("tool not provisioned")]
    assert len(payload["model"]["toolIds"]) == 3  # all three resolved, none dangling


# ── G3. voice/model/keyterms ONCE on the budtender assistant payload (ADR-011) ──
@pytest.mark.django_db
def test_budtender_payload_emits_voice_model_once(fake_vapi, p1_prompts):
    for t in ("suggest_products", "check_inventory", "pair_upsell"):
        provision.ensure_tool(t)
    payload, _ = provision.build_assistant_payload("budtender", name="budtender")
    dumped = json.dumps(payload)
    assert dumped.count('"provider": "cartesia"') == 1
    assert dumped.count('"provider": "deepgram"') == 1
    assert dumped.count('"provider": "openai"') == 1
    assert dumped.count('"keyterm"') == 1
    assert payload["model"]["model"] == "gpt-4.1-mini"  # ADR-010 single model
    assert "Koptza" in payload["model"]["messages"][0]["content"]


# ── squad: the entry_router →(retail)→ budtender edge resolves ──────────────────
@pytest.mark.django_db
def test_squad_has_entry_router_to_budtender_edge(fake_vapi, p1_prompts):
    provision.provision_all(dry_run=False)
    members = provision._provisioned_members()
    payload = provision.build_squad_payload(members)
    # find the entry_router member and assert it can reach budtender
    er = next(m for m in payload["members"] if m["assistantId"] == members["entry_router"])
    dest_names = {d["assistantName"] for d in er["assistantDestinations"]}
    assert "budtender" in dest_names  # retail intent edge
    # and the faq edge resolves to the merged entry_faq assistant name, not the bare role
    assert "entry_faq" in dest_names


# ── G2. idempotency: a re-run is zero-drift ─────────────────────────────────────
@pytest.mark.django_db
def test_provision_p1_rerun_is_zero_drift(fake_vapi, p1_prompts):
    r1 = provision.provision_all(dry_run=False)
    assert r1.ok
    assert (
        r1.created >= 5
    )  # faq_lookup + 3 suggest tools + entry_faq/entry_router/budtender + squad

    fake_vapi.creates = 0
    fake_vapi.patches = 0
    r2 = provision.provision_all(dry_run=False)
    assert r2.ok
    assert fake_vapi.creates == 0  # zero new Vapi objects on re-run (A-IDEMP)
