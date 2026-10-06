"""Per-store Vapi squads: each store gets its own squad (store-locked prompt, store-only transfer,
its own greeting) and the dialed number — not the model — decides which store a tool reads."""

from __future__ import annotations

import json

import pytest

from voice import provision
from voice import webhooks as W

STORE_MAP = {"pn_y": "yakima", "pn_m": "mount-vernon", "pn_p": "pullman"}


@pytest.fixture
def seeded(db):
    from kb.models import AgentPrompt

    for role, first in (("entry_router", "Welcome to Happy Time {store_name}! What can I do?"), ("escalation", ""), ("faq", "")):
        AgentPrompt.objects.create(role=role, body=f"{role} body", first_message=first, tool_names=[], is_active=True)
    return {"entry_router": "a1", "escalation": "a2", "faq": "a3"}


def _override(squad, assistant_id):
    return next(m for m in squad["members"] if m["assistantId"] == assistant_id)["assistantOverrides"]


def test_store_squad_has_its_own_name_and_lock_line(seeded):
    squad = provision.build_squad_payload(seeded, "pullman")
    assert squad["name"].endswith("Pullman")
    body = _override(squad, "a3")["model"]["messages"][0]["content"]
    assert "Pullman store's own phone line" in body
    assert "Yakima" not in body


def test_greeting_is_per_store(seeded):
    y = _override(provision.build_squad_payload(seeded, "yakima"), "a1")["firstMessage"]
    m = _override(provision.build_squad_payload(seeded, "mount-vernon"), "a1")["firstMessage"]
    assert "Happy Time Yakima!" in y and "Happy Time Mount Vernon!" in m
    assert "{store_name}" not in y + m


def test_website_greeting_drops_the_token(seeded):
    assert provision.entry_greeting() == "Welcome to Happy Time! What can I do?"


def test_store_transfer_has_only_that_stores_destination(seeded, settings):
    settings.HHT_TRANSFER_NUMBER_YAKIMA = "+15095550001"
    settings.HHT_TRANSFER_NUMBER_PULLMAN = "+15095550003"
    squad = provision.build_squad_payload(seeded, "pullman")
    dests = _override(squad, "a2")["model"]["tools"][0]["destinations"]
    assert [d["number"] for d in dests] == ["+15095550003"]


def test_shared_squad_is_unchanged(seeded):
    squad = provision.build_squad_payload(seeded)
    assert squad["name"] == "Happy Time Voice"
    assert all("assistantOverrides" not in m for m in squad["members"])
    assert len(provision._transfer_tool([])["destinations"]) == 3


def test_store_phone_numbers_reverses_the_map(settings):
    settings.VAPI_PHONE_NUMBER_STORE_MAP = json.dumps(STORE_MAP)
    assert provision.store_phone_numbers() == {"yakima": "pn_y", "mount-vernon": "pn_m", "pullman": "pn_p"}


def _tool_msg(pn_id, store_arg):
    return {
        "type": "tool-calls",
        "call": {"id": "c1", "phoneNumberId": pn_id},
        "toolCalls": [{"id": "t1", "function": {"name": "check_inventory", "arguments": {"store": store_arg}}}],
    }


def test_mapped_number_overrides_the_models_store_arg(settings, monkeypatch):
    settings.VAPI_PHONE_NUMBER_STORE_MAP = json.dumps(STORE_MAP)
    seen = []
    monkeypatch.setattr(W, "dispatch_tool", lambda name, args, ctx: seen.append(args["store"]) or {"ok": True})
    monkeypatch.setattr(W, "_log_tool_call", lambda *a, **k: None)
    W.handle_tool_calls(_tool_msg("pn_p", "yakima"))  # the Pullman line; model asks for Yakima
    assert seen == ["pullman"]


def test_unmapped_number_keeps_the_models_store_arg(settings, monkeypatch):
    settings.VAPI_PHONE_NUMBER_STORE_MAP = ""
    seen = []
    monkeypatch.setattr(W, "dispatch_tool", lambda name, args, ctx: seen.append(args["store"]) or {"ok": True})
    monkeypatch.setattr(W, "_log_tool_call", lambda *a, **k: None)
    W.handle_tool_calls(_tool_msg("pn_other", "mount-vernon"))  # legacy shared line: model still picks
    assert seen == ["mount-vernon"]
