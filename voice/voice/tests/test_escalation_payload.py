"""P2 — the escalation provisioning payloads (12-P2 §7 A1/A2/B1/B2).

The orphan fix: the escalation member gains REAL inbound edges (entry_router/budtender/faq →
escalation) and a NON-EMPTY warm transferCall (the export had zero edges + `destinations: []`).
No network — these assert the code-built payload shapes (Vapi client never called).
"""

from __future__ import annotations

import pytest

from kb import seed
from voice import constants as C
from voice.provision import build_assistant_payload, build_squad_payload


@pytest.fixture
def _seeded(db):
    seed.seed_agent_prompts()


# ── A2 + B1 + B2: the warm transferCall on the escalation assistant ────────────


@pytest.mark.django_db
def test_escalation_transfer_tool_warm_with_transcript(settings, _seeded):
    """A2: non-empty destinations; B1: warm mode + {{transcript}} summaryPlan; number from env."""
    settings.HHT_TRANSFER_NUMBER_YAKIMA = "+15095711106"
    payload, warnings = build_assistant_payload("escalation", name="escalation")
    tools = payload["model"].get("tools", [])
    transfer = [t for t in tools if t["type"] == "transferCall"]
    assert transfer, "escalation must carry a transferCall tool"
    dests = transfer[0]["destinations"]
    assert dests, "destinations must be non-empty (the export had [])"
    dest = dests[0]
    assert dest["number"] == "+15095711106"
    plan = dest["transferPlan"]
    assert plan["mode"] == "warm-transfer-say-summary"  # B1
    sp = plan["summaryPlan"]
    assert sp["enabled"] is True
    assert "{{transcript}}" in sp["messages"][0]["content"]  # operator hears context
    assert not warnings  # number configured → no placeholder warning


@pytest.mark.django_db
def test_escalation_transfer_placeholder_degrade(settings, _seeded):
    """A2: an unset HHT_TRANSFER_NUMBER_<KEY> degrades to a documented placeholder + a warning,
    never a crash."""
    settings.HHT_TRANSFER_NUMBER_YAKIMA = ""
    payload, warnings = build_assistant_payload("escalation", name="escalation")
    dest = payload["model"]["tools"][0]["destinations"][0]
    assert dest["number"] == C.TRANSFER_NUMBER_PLACEHOLDER
    assert any("transfer number not configured" in w for w in warnings)


@pytest.mark.django_db
def test_transfer_tool_introduces_no_per_node_voice_model(settings, _seeded):
    """B2: the transferCall block sets voice/transcriber/model NOWHERE (they're member-level)."""
    payload, _ = build_assistant_payload("escalation", name="escalation")
    transfer = payload["model"]["tools"][0]
    assert set(transfer.keys()) == {"type", "destinations"}
    dest = transfer["destinations"][0]
    for forbidden in ("voice", "transcriber", "model", "provider"):
        assert forbidden not in dest
        assert forbidden not in dest["transferPlan"]


# ── A1: the three inbound edges into escalation (the orphan fix) ───────────────


@pytest.mark.django_db
def test_squad_has_three_inbound_edges_into_escalation(_seeded):
    """A1: entry_router→escalation, budtender→escalation, faq→escalation, each with a description.
    The export's escalation node had ZERO inbound edges."""
    # All five members resolvable by name → every edge is emitted.
    member_names = {
        "faq": "id_faq",
        "entry_router": "id_entry",
        "budtender": "id_bud",
        "vendor": "id_vendor",
        "escalation": "id_esc",
    }
    squad = build_squad_payload(member_names)
    edges = {}
    for member, role in (
        (squad["members"][_role_index(squad, "id_entry")], "entry_router"),
        (squad["members"][_role_index(squad, "id_bud")], "budtender"),
        (squad["members"][_role_index(squad, "id_faq")], "faq"),
    ):
        names = [d["assistantName"] for d in member["assistantDestinations"]]
        edges[role] = names

    assert "escalation" in edges["entry_router"]
    assert "escalation" in edges["budtender"]
    assert "escalation" in edges["faq"]

    # Each escalation edge carries a non-empty trigger description.
    for member in squad["members"]:
        for dest in member["assistantDestinations"]:
            if dest["assistantName"] == "escalation":
                assert dest["description"], "the escalation edge needs a trigger description"


@pytest.mark.django_db
def test_escalation_member_is_terminal(_seeded):
    """The escalation member itself has no further assistant handoff (terminal; warm transfer out)."""
    member_names = {
        "faq": "id_faq",
        "entry_router": "id_entry",
        "budtender": "id_bud",
        "vendor": "id_vendor",
        "escalation": "id_esc",
    }
    squad = build_squad_payload(member_names)
    esc = squad["members"][_role_index(squad, "id_esc")]
    assert esc["assistantDestinations"] == []


def _role_index(squad, assistant_id):
    for i, m in enumerate(squad["members"]):
        if m["assistantId"] == assistant_id:
            return i
    raise AssertionError(f"{assistant_id} not in squad")
