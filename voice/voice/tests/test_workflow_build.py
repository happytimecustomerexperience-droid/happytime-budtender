"""ADR-023 — the guided-questionnaire Vapi Workflow builder (voice/workflow.py).

Offline structural checks: the DTO is well-formed (one start, no dangling edges, unique names),
every question node reliably extracts a variable, the tool nodes reuse the existing webhook tools
(no prompt — the shape the live API enforces), and the model is the ONE intentional model. No network.
"""

from __future__ import annotations

import json

import pytest

from voice import constants as C
from voice import workflow


@pytest.fixture
def payload(db, settings):
    settings.PUBLIC_BASE_URL = "https://voice.happytimeweed.com"
    settings.VAPI_WEBHOOK_SECRET = "test-secret"
    settings.HHT_TRANSFER_NUMBER_YAKIMA = "+15095711106"
    p, _warnings = workflow.build_workflow_payload()
    return p


@pytest.mark.django_db
def test_structurally_sound(payload):
    """One start node, unique names, no edge points at a missing node (validate_payload is clean)."""
    assert workflow.validate_payload(payload) == []


@pytest.mark.django_db
def test_one_intentional_model_and_globals_once(payload):
    """Model is gpt-4.1-mini (never the shadowed gpt-5.2); voice/transcriber/model are workflow-level
    (emitted once, not per node — ADR-011)."""
    assert payload["model"]["model"] == "gpt-4.1-mini"
    dumped = json.dumps(payload)
    assert "gpt-5.2-chat-latest" not in dumped
    # No node redefines voice/transcriber/model (they live only at the top level).
    for node in payload["nodes"]:
        assert "voice" not in node and "transcriber" not in node and "model" not in node
    assert payload["voice"]["provider"] == "cartesia"
    assert payload["transcriber"]["keyterm"] == C.DEEPGRAM_KEYTERMS


@pytest.mark.django_db
def test_every_question_node_extracts_a_variable(payload):
    """The reliability core: each conversation node that asks a question captures a typed variable via
    variableExtractionPlan (the 'present'/wrap nodes capture chosen_sku/order_confirmed too)."""
    conv = [n for n in payload["nodes"] if n["type"] == "conversation"]
    # welcome + pick_store + every branch question/select/wrap = many; all but none should be bare.
    missing = [n["name"] for n in conv if "variableExtractionPlan" not in n]
    assert missing == [], f"conversation nodes with no extraction: {missing}"


@pytest.mark.django_db
def test_tool_nodes_reuse_webhook_tools_with_no_prompt(payload):
    """tool nodes call the SAME function tools the squad uses (server.url = our webhook), and carry NO
    prompt (the live API rejects a prompt on a tool node)."""
    tools = [n for n in payload["nodes"] if n["type"] == "tool"]
    fn_names = {n["tool"].get("function", {}).get("name") for n in tools if n["tool"].get("function")}
    # the four webhook tools the questionnaire needs are all present
    for needed in ("suggest_products", "check_inventory", "pair_upsell", "notify_staff_issue"):
        assert needed in fn_names, f"{needed} tool node missing"
    for n in tools:
        assert "prompt" not in n
        if n["tool"].get("type") == "function":
            assert n["tool"]["server"]["url"].endswith("/api/voice/vapi")


@pytest.mark.django_db
def test_category_fans_to_all_five_with_cartridge_through_concentrate(payload):
    """Routing: pick_store fans to flower/concentrate/edible/tincture; cartridge enters the concentrate
    branch (the export has no cart effect node) and the cartridge battery question rejoins the tail."""
    froms = [(e["from"], e["to"]) for e in payload["edges"]]
    store_targets = {to for (frm, to) in froms if frm == "pick_store"}
    assert {"flower_effect", "conc_effect", "ed_effect", "tinc_effect"} <= store_targets
    # cartridge: check_conc -> cart_battery -> upsell_conc (rejoin)
    assert ("check_conc", "cart_battery") in froms
    assert ("cart_battery", "upsell_conc") in froms


@pytest.mark.django_db
def test_escalation_is_global_and_emails_then_optionally_transfers(payload):
    """Escalation is enterable from anywhere (globalNodePlan) and its default path emails the team
    (notify_staff_issue) before any transfer — the gather+email default (ADR-016/017)."""
    esc = next(n for n in payload["nodes"] if n["name"] == "escalation")
    assert esc["globalNodePlan"]["enabled"] is True
    assert esc["globalNodePlan"]["enterCondition"]
    froms = {(e["from"], e["to"]) for e in payload["edges"]}
    assert ("escalation", "notify_staff_issue") in froms  # email first
    assert ("notify_staff_issue", "escalation_sent") in froms
    assert ("escalation_sent", "transfer") in froms  # human only after the email + a repeat ask
