"""P3 — the vendor member is B2B-only (13-P3 §7 E1/E2; §8.2).

E1: the ``vendor`` assistant payload carries ONLY notify_vendor_callback (+ the native
transferCall) — NO suggest_products/check_inventory/pair_upsell. E2: the squad has no
vendor→budtender / vendor→faq destination — only vendor→escalation. Vapi HTTP MOCKED; offline.
"""

from __future__ import annotations

import json

import pytest

from core.services import vapi
from voice import provision
from voice.tests.test_provision import FakeAccount

_PRODUCT_TOOLS = ("suggest_products", "check_inventory", "pair_upsell")


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
def test_vendor_payload_has_no_product_tool(fake_vapi, all_prompts):
    """E1: the vendor assistant payload attaches only notify_vendor_callback + a transferCall."""
    # Provision the tools first so the vendor tool id resolves (no dangling toolId).
    provision.provision_all(dry_run=False)
    payload, warnings = provision.build_assistant_payload("vendor", name="vendor")

    assert not [w for w in warnings if w.startswith("tool not provisioned")]
    dumped = json.dumps(payload)
    # The warm transferCall is present exactly once.
    assert dumped.count('"type": "transferCall"') == 1
    # No product tool name appears anywhere in the vendor payload.
    for product_tool in _PRODUCT_TOOLS:
        assert product_tool not in dumped


@pytest.mark.django_db
def test_squad_vendor_only_escalation_edge(fake_vapi, all_prompts):
    """E2: the squad has vendor→escalation and NO vendor→budtender / vendor→faq edge."""
    provision.provision_all(dry_run=False)
    members = provision._provisioned_members()
    payload = provision.build_squad_payload(members)

    vendor_member = next(x for x in payload["members"] if x["assistantId"] == members["vendor"])
    dests = {d["assistantName"] for d in vendor_member["assistantDestinations"]}
    assert dests == {"escalation"}
    assert "budtender" not in dests
    assert "faq" not in dests
