"""The flow-canvas graph layer (14-P4 §3.3) — config + docs ONLY, fail-closed.

The canvas DOCUMENTS the live Squad shape + per-transition trigger conditions; it NEVER edits a
runtime guardrail. ``clean_graph`` is the fail-closed enforcement boundary (ported + tightened from
swedish-bot ``dashboard/views.py::_clean_graph``):

  * role allowlist = exactly the 5 Squad members (``_AGENT_ROLES``) — an unknown role is rejected.
  * node-kind allowlist = the Vapi node kinds (``NODE_KINDS``) — an unknown kind is rejected.
  * MAX_NODES=80 / MAX_EDGES=160 / MAX_COLLECT=30 size caps; char caps on every string.
  * coords clamped to ``[0, 6000]`` (never rejected — clamped, per B2).

The runtime Squad topology is CODE-owned (``voice/constants.SQUAD_SHAPE``); a Publish re-asserts the
required transitions from code, so a canvas edit cannot delete a required transition or a guardrail.
"""

from __future__ import annotations

# Vapi node kinds the canvas can place (docs/config only).
NODE_KINDS = ["agent", "handoff", "tool", "transfer", "end"]
MAX_NODES, MAX_EDGES, MAX_COLLECT = 80, 160, 30
# The role allowlist = exactly the 5 Squad members (fail-closed in clean_graph).
_AGENT_ROLES = {"entry_router", "budtender", "faq", "vendor", "escalation"}

# Replaces swedish-bot AGENT_FLOW — the staff-facing blurb + step per Squad member.
VOICE_AGENT_FLOW = {
    "entry_router": {
        "step": "1",
        "blurb": "Greets as Happy Time, captures the store + confirms 21+, classifies intent in "
        "one turn → budtender / faq / vendor / escalation.",
    },
    "budtender": {
        "step": "2",
        "blurb": "Slot-fills, calls suggest_products / check_inventory / pair_upsell; speaks OTD "
        "prices + why_this; one gated upsell.",
    },
    "faq": {
        "step": "2",
        "blurb": "Grounded answers from the KB (hours/returns/limits/payment/pickup/weights-types). "
        "Numbers come from KB rows only.",
    },
    "vendor": {
        "step": "2",
        "blurb": "Never retail. Warm-transfers to the store; on no-answer captures the reason → "
        "VendorCallback + staff alert + callback window.",
    },
    "escalation": {
        "step": "✓",
        "blurb": "≥2 human requests / return dispute / defective return → warm transferCall with "
        "{{transcript}} summary to the per-location number.",
    },
}


def _coord(v):
    try:
        return max(0, min(6000, round(float(v))))
    except (TypeError, ValueError):
        return 0


def clean_graph(data):
    """Whitelist + validate the posted graph (fail-closed). Returns ``(graph, error)``."""
    if not isinstance(data, dict):
        return None, "graph must be an object"
    raw_nodes, raw_edges = data.get("nodes"), data.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        return None, "nodes and edges must be lists"
    if len(raw_nodes) > MAX_NODES or len(raw_edges) > MAX_EDGES:
        return None, "graph too large"

    def s(v, n=400):
        return str(v if v is not None else "")[:n]

    nodes, ids = [], set()
    for n in raw_nodes:
        if not isinstance(n, dict):
            return None, "bad node"
        nid = s(n.get("id"), 60).strip()
        kind = s(n.get("kind"), 20)
        if not nid or nid in ids or kind not in NODE_KINDS:
            return None, f"invalid node {nid!r}"
        ids.add(nid)
        node = {
            "id": nid,
            "kind": kind,
            "title": s(n.get("title"), 80),
            "x": _coord(n.get("x")),
            "y": _coord(n.get("y")),
        }
        if kind == "agent":
            role = s(n.get("role"), 32)
            if role not in _AGENT_ROLES:
                return None, f"unknown agent role {role!r}"
            node["role"] = role
        cfg = n.get("config")
        node["config"] = (
            {s(k, 40): s(v, 2000) for k, v in cfg.items()} if isinstance(cfg, dict) else {}
        )
        nodes.append(node)

    edges = []
    for e in raw_edges:
        if not isinstance(e, dict):
            return None, "bad edge"
        src, tgt = s(e.get("source"), 60), s(e.get("target"), 60)
        if src not in ids or tgt not in ids:
            return None, "edge references missing node"
        collect = []
        rc = e.get("collect")
        if isinstance(rc, list):
            for f in rc[:MAX_COLLECT]:
                if isinstance(f, dict) and s(f.get("name"), 60).strip():
                    collect.append(
                        {
                            "name": s(f.get("name"), 60),
                            "label": s(f.get("label"), 80),
                            "required": bool(f.get("required")),
                        }
                    )
        edges.append(
            {
                "id": s(e.get("id"), 60) or f"{src}->{tgt}",
                "source": src,
                "target": tgt,
                "label": s(e.get("label"), 80),
                "collect": collect,
            }
        )
    return {"nodes": nodes, "edges": edges}, None


def default_flow_graph() -> dict:
    """Seed the canvas to MIRROR the live Squad (01-ARCHITECTURE §1.6): the 5 members as
    ``kind:"agent"`` nodes + a ``transfer`` terminal + ``handoff`` edges carrying the trigger
    condition as the edge label. Informational only — the canvas does not drive runtime."""
    nodes = [
        {
            "id": "entry_router",
            "kind": "agent",
            "role": "entry_router",
            "title": "Entry router",
            "x": 40,
            "y": 200,
            "config": {},
        },
        {
            "id": "budtender",
            "kind": "agent",
            "role": "budtender",
            "title": "Budtender",
            "x": 320,
            "y": 60,
            "config": {},
        },
        {
            "id": "faq",
            "kind": "agent",
            "role": "faq",
            "title": "FAQ",
            "x": 320,
            "y": 200,
            "config": {},
        },
        {
            "id": "vendor",
            "kind": "agent",
            "role": "vendor",
            "title": "Vendor",
            "x": 320,
            "y": 340,
            "config": {},
        },
        {
            "id": "escalation",
            "kind": "agent",
            "role": "escalation",
            "title": "Escalation",
            "x": 600,
            "y": 200,
            "config": {},
        },
        {
            "id": "transfer",
            "kind": "transfer",
            "title": "Warm transfer",
            "x": 860,
            "y": 200,
            "config": {"mode": "warm-transfer-wait-for-operator"},
        },
    ]
    edges = [
        {
            "id": "e1",
            "source": "entry_router",
            "target": "budtender",
            "label": "retail intent",
            "collect": [],
        },
        {
            "id": "e2",
            "source": "entry_router",
            "target": "faq",
            "label": "info intent",
            "collect": [],
        },
        {
            "id": "e3",
            "source": "entry_router",
            "target": "vendor",
            "label": "vendor/wholesale/manifest",
            "collect": [],
        },
        {
            "id": "e4",
            "source": "entry_router",
            "target": "escalation",
            "label": "≥2 human / dispute / defective",
            "collect": [],
        },
        {
            "id": "e5",
            "source": "budtender",
            "target": "escalation",
            "label": "human request mid-flow",
            "collect": [],
        },
        {"id": "e6", "source": "faq", "target": "budtender", "label": "cross-sell", "collect": []},
        {
            "id": "e7",
            "source": "faq",
            "target": "escalation",
            "label": "dispute / human request",
            "collect": [],
        },
        {
            "id": "e8",
            "source": "vendor",
            "target": "escalation",
            "label": "dispute / human request",
            "collect": [],
        },
        {
            "id": "e9",
            "source": "escalation",
            "target": "transfer",
            "label": "warm transfer",
            "collect": [],
        },
    ]
    return {"nodes": nodes, "edges": edges}


def get_flow():
    """Get-or-seed the FlowConfig singleton — seeds the canvas to mirror the live Squad."""
    from kb.models import FlowConfig

    cfg = FlowConfig.objects.first()
    if cfg is None:
        cfg = FlowConfig.objects.create(graph=default_flow_graph())
    elif not (cfg.graph or {}).get("nodes"):
        cfg.graph = default_flow_graph()
        cfg.save(update_fields=["graph", "updated_at"])
    return cfg
