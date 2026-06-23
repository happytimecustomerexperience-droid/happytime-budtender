"""P2 — provisioning the escalation member + the orphan-fixing squad edges (12-P2 §7 G1).

Vapi HTTP layer MOCKED (reuses FakeAccount). Asserts: the escalation assistant is created with a
non-empty warm transferCall; the squad carries the 3 inbound …→escalation edges; a re-run is
zero-drift (no new Vapi objects). No `/workflow` is ever called (the FakeAccount has no such verb).
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
def all_prompts(db, settings):
    """Seed faq + entry_router + budtender + escalation (the full P0→P2 member set)."""
    settings.HHT_TRANSFER_NUMBER_YAKIMA = "+15095711106"
    from kb import seed

    seed.seed_agent_prompts()


@pytest.mark.django_db
def test_provision_creates_escalation_member(fake_vapi, all_prompts):
    """G1: the escalation assistant is created (it has a seeded AgentPrompt + the transferCall)."""
    report = provision.provision_all(dry_run=False)
    assert report.ok
    by = {(r.kind, r.name): r for r in report.results}
    assert by[("assistant", "escalation")].action == "created"
    assert by[("assistant", "escalation")].vapi_id


@pytest.mark.django_db
def test_provisioned_squad_has_inbound_escalation_edges(fake_vapi, all_prompts):
    """A1: after provision the squad payload carries entry_router/budtender/faq → escalation."""
    provision.provision_all(dry_run=False)
    members = provision._provisioned_members()
    payload = provision.build_squad_payload(members)

    def _dests(role):
        m = next(x for x in payload["members"] if x["assistantId"] == members[role])
        return {d["assistantName"] for d in m["assistantDestinations"]}

    assert "escalation" in _dests("entry_router")
    assert "escalation" in _dests("budtender")
    assert "escalation" in _dests("faq")


@pytest.mark.django_db
def test_provision_p2_rerun_is_zero_drift(fake_vapi, all_prompts):
    """G1: a re-run creates zero new Vapi objects + issues zero PATCHes (zero drift)."""
    r1 = provision.provision_all(dry_run=False)
    assert r1.ok and r1.created >= 6  # tools + 4 assistants + squad

    fake_vapi.creates = 0
    fake_vapi.patches = 0
    r2 = provision.provision_all(dry_run=False)
    assert r2.ok
    assert fake_vapi.creates == 0
    assert fake_vapi.patches == 0  # truly drift-free


@pytest.mark.django_db
def test_escalation_payload_carries_transfer_and_staff_issue_tool(fake_vapi, all_prompts):
    """The escalation payload has the warm transferCall + its notify_staff_issue toolId (the
    gather+email default), with no dangling tool warning."""
    from voice.models import VapiObject

    VapiObject.objects.update_or_create(
        kind="tool", name="notify_staff_issue", defaults={"vapi_id": "id_notify_staff_issue"}
    )
    payload, warnings = provision.build_assistant_payload("escalation", name="escalation")
    assert not [w for w in warnings if w.startswith("tool not provisioned")]
    dumped = json.dumps(payload)
    assert dumped.count('"type": "transferCall"') == 1
    assert payload["model"]["toolIds"] == ["id_notify_staff_issue"]  # gather+email tool
