"""P3 — provisioning the vendor tool + member + edges (13-P3 §7 G; §8.3). Vapi HTTP MOCKED.

G1: provision creates the notify_vendor_callback tool + the vendor assistant + the
entry_router→vendor / vendor→escalation edges; ids written back. G2: a 2nd run issues ZERO new
Vapi objects + zero PATCHes (zero drift, ADR-003). G3: the vendor payload sets voice/transcriber/
model ONCE (no per-node dup — export #7). Offline, no live keys; no /workflow is ever called.
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
    settings.HHT_TRANSFER_NUMBER_YAKIMA = "+15095711106"
    from kb import seed

    seed.seed_agent_prompts()


@pytest.mark.django_db
def test_provision_creates_vendor_tool_and_member(fake_vapi, all_prompts):
    """G1: the notify_vendor_callback tool + the vendor assistant are created with ids."""
    report = provision.provision_all(dry_run=False)
    assert report.ok
    by = {(r.kind, r.name): r for r in report.results}

    assert by[("tool", "notify_vendor_callback")].action == "created"
    assert by[("tool", "notify_vendor_callback")].vapi_id
    assert by[("assistant", "vendor")].action == "created"
    assert by[("assistant", "vendor")].vapi_id


@pytest.mark.django_db
def test_provisioned_squad_has_vendor_edges(fake_vapi, all_prompts):
    """G1: the squad carries entry_router→vendor and vendor→escalation."""
    provision.provision_all(dry_run=False)
    members = provision._provisioned_members()
    payload = provision.build_squad_payload(members)

    def _dests(role):
        m = next(x for x in payload["members"] if x["assistantId"] == members[role])
        return {d["assistantName"] for d in m["assistantDestinations"]}

    assert "vendor" in _dests("entry_router")
    assert "escalation" in _dests("vendor")


@pytest.mark.django_db
def test_provision_vendor_rerun_is_zero_drift(fake_vapi, all_prompts):
    """G2: a re-run creates zero new Vapi objects + issues zero PATCHes."""
    r1 = provision.provision_all(dry_run=False)
    assert r1.ok and r1.created >= 7  # 6 tools + 5 assistants + squad

    fake_vapi.creates = 0
    fake_vapi.patches = 0
    r2 = provision.provision_all(dry_run=False)
    assert r2.ok
    assert fake_vapi.creates == 0
    assert fake_vapi.patches == 0


@pytest.mark.django_db
def test_vendor_payload_sets_voice_model_once(fake_vapi, all_prompts):
    """G3: the vendor assistant payload sets voice/transcriber/model ONCE (no per-node dup)."""
    payload, _warnings = provision.build_assistant_payload("vendor", name="vendor")
    dumped = json.dumps(payload)
    # voiceId appears exactly once; the model id once; the keyterm list once.
    assert dumped.count("a3520a8f-226a-428d-9fcd-b0a4711a6829") == 1
    assert dumped.count('"model": "gpt-4.1-mini"') == 1
    assert dumped.count('"nova-3"') == 1
    # the model + voice + transcriber blocks each appear once at the top level.
    assert list(payload.keys()).count("voice") == 1
    assert list(payload.keys()).count("transcriber") == 1


@pytest.mark.django_db
def test_no_workflow_endpoint_called(fake_vapi, all_prompts):
    """The FakeAccount has no /workflow verb — provisioning only touches assistant/squad/tool."""
    provision.provision_all(dry_run=False)
    assert not hasattr(fake_vapi, "create_workflow")
