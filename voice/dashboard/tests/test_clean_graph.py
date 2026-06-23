"""Fail-closed validator tests for the flow canvas (14-P4 §7 B1/B2; §8 unit).

The canvas is config + docs only — ``clean_graph`` is the boundary that makes a safety guardrail /
required transition impossible to inject from the UI. Ported from swedish-bot's ``_clean_graph``
tests, retargeted to the 5 voice roles + the Vapi node kinds. Offline, no DB.
"""

from __future__ import annotations

from dashboard.flowgraph import MAX_NODES, clean_graph


def _agent(nid, role, **kw):
    return {"id": nid, "kind": "agent", "role": role, "x": 10, "y": 10, **kw}


def test_valid_graph_round_trips():
    graph = {
        "nodes": [_agent("a", "entry_router"), _agent("b", "budtender")],
        "edges": [{"id": "e1", "source": "a", "target": "b", "label": "retail"}],
    }
    cleaned, err = clean_graph(graph)
    assert err is None
    assert len(cleaned["nodes"]) == 2
    assert cleaned["edges"][0]["source"] == "a"


# ── B1: role allowlist (fail-closed) ────────────────────────────────────────────
def test_unknown_agent_role_rejected():
    graph = {"nodes": [_agent("a", "hacker")], "edges": []}
    cleaned, err = clean_graph(graph)
    assert cleaned is None
    assert "unknown agent role" in err


def test_each_of_the_five_roles_is_allowed():
    for role in ("entry_router", "budtender", "faq", "vendor", "escalation"):
        cleaned, err = clean_graph({"nodes": [_agent("a", role)], "edges": []})
        assert err is None, role


# ── B2: node-kind allowlist + size caps + coord clamp ───────────────────────────
def test_unknown_node_kind_rejected():
    graph = {"nodes": [{"id": "a", "kind": "wormhole", "x": 0, "y": 0}], "edges": []}
    cleaned, err = clean_graph(graph)
    assert cleaned is None
    assert "invalid node" in err


def test_too_many_nodes_rejected():
    nodes = [_agent(f"n{i}", "faq") for i in range(MAX_NODES + 1)]
    cleaned, err = clean_graph({"nodes": nodes, "edges": []})
    assert cleaned is None
    assert err == "graph too large"


def test_out_of_bounds_coords_are_clamped_not_rejected():
    graph = {"nodes": [_agent("a", "faq", x=99999, y=-50)], "edges": []}
    cleaned, err = clean_graph(graph)
    assert err is None
    assert cleaned["nodes"][0]["x"] == 6000  # clamped to the [0, 6000] band
    assert cleaned["nodes"][0]["y"] == 0


def test_oversized_prompt_strings_are_truncated():
    huge = "x" * 5000
    graph = {"nodes": [_agent("a", "faq", title=huge, config={"note": huge})], "edges": []}
    cleaned, err = clean_graph(graph)
    assert err is None
    assert len(cleaned["nodes"][0]["title"]) <= 80
    assert len(cleaned["nodes"][0]["config"]["note"]) <= 2000


def test_edge_to_missing_node_rejected():
    graph = {"nodes": [_agent("a", "faq")], "edges": [{"source": "a", "target": "ghost"}]}
    cleaned, err = clean_graph(graph)
    assert cleaned is None
    assert "missing node" in err


def test_non_dict_rejected():
    cleaned, err = clean_graph(["not", "a", "dict"])
    assert cleaned is None
    assert err
