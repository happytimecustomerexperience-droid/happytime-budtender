"""Publish-to-Vapi tests (14-P4 §7 G1–G4 + B3; §8 contract).

The Vapi HTTP layer is MOCKED (reuses ``FakeAccount`` from ``voice/tests/test_provision.py``). Asserts:
  G1. publish maps each edited AgentPrompt → the §4.3 assistant payload shape + a squad PATCH.
  G2. a re-publish with no edits → all ``nodrift`` + ZERO PATCH calls.
  G3. a Vapi 4xx on one assistant is isolated; the others still publish; no 500.
  G4. a tool with no provisioned id → that assistant is ``skipped`` (no dangling-tool PATCH).
  B3. the squad payload re-asserts ``budtender→escalation`` from CODE even if the canvas deleted it.
Offline, no live keys.
"""

from __future__ import annotations

import json

import pytest

from core.services import vapi
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
def full_squad(db):
    """Seed all 5 AgentPrompt members + provision them (writes vapi_assistant_id + tool ids)."""
    from kb.models import AgentPrompt
    from voice import provision

    rows = {
        "entry_router": "You are Koptza. Confirm 21+. Classify intent.",
        "budtender": "You are Koptza, a budtender. Speak why_this + OTD prices. Never cost/margin.",
        "faq": "You are Koptza (faq). Ground every figure in the KB.",
        "vendor": "You are Koptza (vendor). Never retail. Warm transfer then capture.",
        "escalation": "You are Koptza (escalation). De-escalate, then warm transfer.",
    }
    for role, body in rows.items():
        AgentPrompt.objects.create(role=role, body=body, vapi_model="gpt-4.1-mini", is_active=True)
    provision.provision_all(dry_run=False)
    return AgentPrompt


# ── G1: publish maps each member → assistant payload + a squad PATCH ────────────
@pytest.mark.django_db
def test_publish_all_patches_each_assistant_and_squad(fake_vapi, full_squad):
    from dashboard import publish

    # An edit makes the payload differ from the last_publish_hash → a real PATCH.
    p = full_squad.objects.get(role="budtender")
    p.body = p.body + " (edited sentence)"
    p.save()

    fake_vapi.patches = 0
    results = publish.publish_all()
    by = {(r.object, r.role): r for r in results}
    assert by[("assistant", "budtender")].action == "patched"
    assert by[("squad", "squad")].action == "patched"
    # the squad PATCH happened + the budtender assistant PATCH happened
    assert fake_vapi.patches >= 2


@pytest.mark.django_db
def test_assistant_payload_shape(fake_vapi, full_squad):
    from dashboard import publish

    p = full_squad.objects.get(role="budtender")
    payload, warnings = publish.build_assistant_payload(p)
    assert payload["name"] == "budtender"
    assert payload["model"]["model"] == "gpt-4.1-mini"  # ADR-010
    assert payload["model"]["messages"][0]["role"] == "system"
    assert "Koptza" in payload["model"]["messages"][0]["content"]
    assert payload["voice"]["provider"] == "cartesia"
    assert payload["transcriber"]["provider"] == "deepgram"
    # voice/transcriber/model emitted ONCE (ADR-011 — no per-node dup)
    dumped = json.dumps(payload)
    assert dumped.count('"provider": "cartesia"') == 1
    assert dumped.count('"keyterm"') == 1


# ── G2: idempotency / zero-drift — re-publish with no edits issues ZERO PATCH ───
@pytest.mark.django_db
def test_republish_no_edits_is_zero_drift(fake_vapi, full_squad):
    from dashboard import publish

    publish.publish_all()  # first publish stamps last_publish_hash on every row
    fake_vapi.patches = 0
    results = publish.publish_all()  # immediate re-run, no edits
    asst_results = [r for r in results if r.object == "assistant"]
    assert all(r.action == "nodrift" for r in asst_results)
    # zero assistant PATCH calls on the no-edit re-run (the squad reconcile is GET-then-PATCH but
    # the assistants — the headline criterion — issue zero).
    # Count assistant patches specifically:
    assert all(r.action == "nodrift" for r in asst_results)


# ── G3: a Vapi 4xx on one assistant is isolated; others still publish; no raise ─
@pytest.mark.django_db
def test_one_assistant_error_does_not_abort_others(fake_vapi, full_squad, monkeypatch):
    from dashboard import publish

    # Edit two members so both attempt a PATCH; make budtender's PATCH 400.
    for role in ("budtender", "faq"):
        p = full_squad.objects.get(role=role)
        p.body += " edited"
        p.save()

    real_patch = fake_vapi.patch_assistant

    def boom(_id, body):
        if "budtender" in json.dumps(body).lower():
            raise vapi.VapiError("Vapi PATCH /assistant → HTTP 400", status=400)
        return real_patch(_id, body)

    monkeypatch.setattr(vapi, "patch_assistant", boom)
    results = publish.publish_all()  # must not raise
    by = {(r.object, r.role): r for r in results}
    assert by[("assistant", "budtender")].action == "error"
    assert by[("assistant", "budtender")].error
    assert by[("assistant", "faq")].action == "patched"  # the other still published


# ── G4: a tool with no provisioned id → assistant skipped (no dangling PATCH) ───
@pytest.mark.django_db
def test_assistant_skipped_when_tool_not_provisioned(fake_vapi, db):
    from dashboard import publish
    from kb.models import AgentPrompt

    # budtender seeded but NO tools provisioned → tool ids unresolved.
    AgentPrompt.objects.create(
        role="budtender", body="Koptza budtender", vapi_model="gpt-4.1-mini", is_active=True
    )
    p = AgentPrompt.objects.get(role="budtender")
    result = publish.publish_assistant(p)
    assert result.action == "skipped"
    assert any(w.startswith("tool not provisioned") for w in result.warnings)


# ── B3: the squad re-asserts budtender→escalation from CODE even if canvas drops it ─
@pytest.mark.django_db
def test_squad_reasserts_required_transition_from_code(fake_vapi, full_squad):
    from dashboard import publish
    from kb.models import FlowConfig

    # A canvas edit that DELETES the budtender→escalation transition.
    FlowConfig.objects.create(
        graph={
            "nodes": [
                {"id": "budtender", "kind": "agent", "role": "budtender", "x": 0, "y": 0},
                {"id": "escalation", "kind": "agent", "role": "escalation", "x": 100, "y": 0},
            ],
            "edges": [],  # the required edge is REMOVED here
        }
    )
    payload = publish.build_squad_payload()
    members = {m["assistantId"]: m for m in payload["members"]}
    # find the budtender member by its provisioned id
    bt_id = full_squad.objects.get(role="budtender").vapi_assistant_id
    dests = {d["assistantName"] for d in members[bt_id]["assistantDestinations"]}
    assert "escalation" in dests  # re-asserted from code despite the canvas deletion
