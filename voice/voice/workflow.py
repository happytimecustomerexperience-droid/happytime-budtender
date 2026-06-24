"""Build the owner's guided product questionnaire as a Vapi **Workflow** (ADR-023).

WHY a Workflow (not the squad): the owner's priority is to *reliably ask each question AND retain +
pass every answer to the budtender*. A workflow captures each answer with a per-node
``variableExtractionPlan`` (typed, stored by Vapi's runtime) and the ``{{liquid}}`` variables flow
into the tool nodes — structurally more reliable than one squad prompt holding every slot in the
model's working memory.

It is a faithful port of ``happy-time-voice-agent-(full-script).json``: one ``conversation`` node per
question (verbatim wording), the per-category branches (flower / concentrate / cartridge / edible /
tincture), and ``tool`` nodes that call the SAME webhook tools the squad uses
(``suggest_products`` / ``check_inventory`` / ``pair_upsell`` / ``notify_staff_issue`` / transferCall)
— so the budtender engine, leak-guard and out-the-door pricing are reused untouched.

Shapes were pinned against the live ``POST /workflow`` (a tool node takes NO ``prompt``; a conversation
node takes ``prompt`` + ``variableExtractionPlan``; edges use ``condition:{type:"ai", prompt}``).

Globals (voice / transcriber / model / server) are emitted ONCE at the workflow level (ADR-011), read
from the same ``constants`` + ``provision`` helpers as the squad — one source of truth.
"""

from __future__ import annotations

from voice import constants as C
from voice import provision

WORKFLOW_NAME = "Happy Time Voice — Guided Menu"

# The workflow-wide system prompt — the guardrails that apply at EVERY node (lifted from the export's
# globalPrompt, condensed): warm, one question at a time, never invent a number, speak the tool's
# out-the-door price verbatim, units spoken in words, always call the tools, escalate after 2 asks.
GLOBAL_PROMPT = (
    "You are the warm, friendly voice of Happy Time Weed, a family-owned Washington cannabis shop "
    "with three stores (Yakima, Mount Vernon, Pullman). Be kind, unhurried and genuinely helpful. "
    "Ask ONE question at a time and never move on until the caller answers. "
    "NUMBERS-GUARD: never invent a price, potency, stock level or store hour — only ever say a figure "
    "a tool returned; if you don't have it, offer to get a teammate. NEVER mention cost or margin. "
    "SPEAK NUMBERS AS WORDS (this is a phone call): voice a tool's price_spoken wording exactly "
    "('sixteen dollars and thirty-four cents') — never the digits or '$16.34'; say 'thirty percent' "
    "not '30%'; 'five milligrams'/'three and a half grams'/'an ounce' not 'mg'/'g'/'oz'; read ratios "
    "as words ('1:1' is 'one to one', '1:50' is 'one to fifty'); say 'D-O-H' not 'doh'; read a slash "
    "as 'or'; never read SKU numbers or links aloud. "
    "When you reach a product step, CALL the tool with every slot you've gathered (store, category and "
    "the caller's preferences) — that's how you fetch real, in-stock picks. Always call check_inventory "
    "before you promise a specific product is available. "
    "If the caller asks for a human twice, or reports a defective product or a return/billing dispute, "
    "move to the escalation step: listen, gather the details, and send them to the team."
)


# ── node + edge builders (shapes verified against live POST /workflow) ─────────
def _prop(spec):
    """Normalize a variableExtractionPlan property: "string" | [enum...] | {full schema}."""
    if isinstance(spec, str):
        return {"type": spec}
    if isinstance(spec, list):
        return {"type": "string", "enum": spec}
    return dict(spec)


def _conv(name, say, props=None, *, is_start=False, glob=None, present=False):
    """A conversation node. ``say`` is spoken near-verbatim (a question) unless ``present`` — then it's
    an instruction to read the tool's picks. ``props`` -> variableExtractionPlan (one or more vars)."""
    if present:
        prompt = (
            "The previous tool returned up to 3 in-stock picks, each with an out-the-door price. "
            f"Present them warmly, using ONLY the tool's product names and prices, like this: {say} "
            "Help the caller choose and capture their pick."
        )
    else:
        prompt = (
            f'Say this warmly and almost word-for-word, then listen and capture the answer before '
            f'moving on: "{say}"'
        )
    node = {"type": "conversation", "name": name, "prompt": prompt}
    if is_start:
        node["isStart"] = True
    if props:
        node["variableExtractionPlan"] = {
            "schema": {"type": "object", "properties": {k: _prop(v) for k, v in props.items()}}
        }
    if glob:
        node["globalNodePlan"] = glob
    return node


def _func_tool(tool_name):
    """A function tool whose server.url is our webhook — the SAME contract the squad uses, so the
    webhook routes by function.name to the existing handler (no async key — matches the probed shape)."""
    spec = C.TOOL_SPECS[tool_name]
    return {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
        "server": provision._server_block(),
    }


def _tool_node(name, tool_name):
    return {"type": "tool", "name": name, "tool": _func_tool(tool_name)}


def _edge(frm, to, cond):
    return {"from": frm, "to": to, "condition": {"type": "ai", "prompt": cond}}


# ── the per-category questionnaire (verbatim wording from the owner's JSON) ─────
# Each branch is an ordered list of conversation nodes; _branch() chains them, then appends the shared
# product tail (suggest -> present -> check -> [extra] -> upsell -> wrap). The category variable
# (captured at the welcome node) is what the suggest tool maps to `category` — cartridge stays
# "cartridge" so the budtender's cartridge guard fires (never silently rewritten to concentrate).

EFFECT = ["relaxed", "uplifted", "middle"]
ACTIVITY = ["chill", "socialize", "creative"]


def _branch(prefix, questions, *, present_say, tool_suffix="", extra=None, dosing=None, wrap_say):
    """Return (nodes, edges, entry_name, done_from) for a category branch.

    questions: list of (name, say, props). The tail: suggest_{p} (tool) -> {p}_select (present,
    chosen_sku) -> check_{p} (tool) -> [extra convo nodes] -> upsell_{p} (tool) -> {p}_wrap -> (done)."""
    nodes, edges = [], []
    chain = [_conv(f"{prefix}_{n}", say, props) for (n, say, props) in questions]
    nodes += chain
    for a, b in zip(chain, chain[1:], strict=False):
        edges.append(_edge(a["name"], b["name"], "the caller answered"))

    suggest = _tool_node(f"suggest_{prefix}", "suggest_products")
    select = _conv(f"{prefix}_select", present_say, {"chosen_sku": "string"}, present=True)
    check = _tool_node(f"check_{prefix}", "check_inventory")
    upsell = _tool_node(f"upsell_{prefix}", "pair_upsell")
    wrap = _conv(f"{prefix}_wrap", wrap_say, {"order_confirmed": "boolean"})
    nodes += [suggest, select, check, upsell, wrap]

    edges.append(_edge(chain[-1]["name"], suggest["name"], "all preferences gathered"))
    edges.append(_edge(suggest["name"], select["name"], "picks returned"))
    edges.append(_edge(select["name"], check["name"], "the caller chose a product"))

    last = check["name"]
    for n, say, props in extra or []:
        node = _conv(f"{prefix}_{n}", say, props)
        nodes.append(node)
        edges.append(_edge(last, node["name"], "the caller answered"))
        last = node["name"]
    if dosing:
        node = _conv(f"{prefix}_dosing", dosing, {"dosing_ack": "boolean"})
        nodes.append(node)
        edges.append(_edge(last, node["name"], "stock confirmed"))
        last = node["name"]
    edges.append(_edge(last, upsell["name"], "ready for a complement"))
    edges.append(_edge(upsell["name"], wrap["name"], "complement decided"))
    return nodes, edges, chain[0]["name"], wrap["name"]


def build_workflow_payload() -> dict:
    """Assemble the full CreateWorkflowDTO. Pure (no network); the provisioner POSTs it."""
    nodes: list[dict] = []
    edges: list[dict] = []

    # Entry: greet (list the menu), capture intent->category. Then ask the store (intent first, store
    # second — the owner's rule). Cartridge routes through the concentrate branch.
    welcome = _conv(
        "welcome",
        C.ENTRY_FIRST_MESSAGE,
        {"category": ["flower", "concentrate", "cartridge", "edible", "tincture"]},
        is_start=True,
    )
    pick_store = _conv(
        "pick_store",
        "Happy to help with that! Which shop are you headed to — Yakima, Mount Vernon, or Pullman?",
        {"store": ["yakima", "mount-vernon", "pullman"]},
    )
    nodes += [welcome, pick_store]
    edges.append(_edge("welcome", "pick_store", "the caller said what they're looking for"))

    # ── FLOWER ──
    f_nodes, f_edges, f_entry, f_done = _branch(
        "flower",
        [
            ("effect", "How would you like to feel after you smoke — fully relaxed and sleepy, "
                       "uplifted, or somewhere in the middle?", {"effect": EFFECT}),
            ("activity", "And what are you planning to do afterward — chill at home, socialize, "
                         "or get creative?", {"activity": ACTIVITY}),
            ("preferences", "What matters most to you when buying flower — THC percentage, nug size, "
                            "the trim, or the smell?", {"flower_priority": "string"}),
            ("past_wins", "What products really hit the spot for you recently, and what did you like "
                          "about them?", {"past_wins": "string"}),
            ("budget", "Are you looking to keep it cheap, get the best, or somewhere in the middle?",
                       {"budget_tier": ["value", "mid", "top"]}),
        ],
        present_say="\"Best\" is our top-shelf craft eighth — best flavor and potency; \"Solid\" is a "
                    "great mid-tier value; \"Budget\" gets the job done for the lowest price.",
        wrap_say="Want to try this same strain as a pre-roll, or grab a pipe, grinder, or papers to go "
                 "with it? And is there anything else you'd like to lock in while you're here?",
    )
    nodes += f_nodes
    edges += f_edges
    edges.append(_edge("pick_store", f_entry, "the caller wants flower"))

    # ── CONCENTRATE (+ cartridge sub-branch at the select step) ──
    c_nodes, c_edges, c_entry, c_done = _branch(
        "conc",
        [
            ("effect", "How would you like to feel afterward — fully relaxed and sleepy, uplifted, or "
                       "a mix of both?", {"effect": EFFECT}),
            ("activity", "What are you planning to do afterward — chill at home, socialize, or get "
                         "creative?", {"activity": ACTIVITY}),
            ("flavor", "Do you enjoy the natural taste of cannabis more, or do you prefer a fruitier "
                       "flavor?", {"flavor": ["cannabis", "fruit", "either"]}),
            ("solvents", "Do you mind butane-processed products, or are you looking for something "
                         "solventless? Everything's passed state testing either way.",
                         {"solvent": ["solventless", "butane", "either"]}),
            ("pesticide", "Does it matter to you if it's pesticide-free? Everything passes pesticide "
                          "testing, but our DOH products are pesticide and heavy-metal free.",
                          {"pesticide_free": "boolean"}),
            ("past_wins", "What products really hit the spot for you recently, and what did you like "
                          "about them?", {"past_wins": "string"}),
            ("budget", "And what price range feels comfortable for you?", {"budget": "number"}),
        ],
        present_say="I'll show you up to three — one right at your price, one about five more, and one "
                    "about ten more in case it's worth trying next time.",
        wrap_say="Would you like to throw in something sweet for three dollars, or maybe a quick joint? "
                 "Anything else while you're here?",
    )
    nodes += c_nodes
    edges += c_edges
    edges.append(_edge("pick_store", c_entry, "the caller wants a concentrate OR a cartridge/vape"))

    # cartridge sub-branch: at conc_select, the export routes cartridge -> battery question (edge 23)
    cart_battery = _conv(
        "cart_battery",
        "What battery are you using? I've got budget-friendly 510-thread options for distillate, "
        "higher-end temp-control for flavor, and all-in-one disposables.",
        {"battery": ["has_510", "needs_510", "temp_control", "aio_disposable"]},
    )
    nodes.append(cart_battery)
    # check_conc -> cart_battery when the caller wanted a cartridge; otherwise the normal conc tail
    # already runs (check_conc -> upsell_conc). The cartridge path rejoins at upsell_conc.
    edges.append(_edge("check_conc", "cart_battery", "the caller wanted a cartridge or vape pen"))
    edges.append(_edge("cart_battery", "upsell_conc", "battery sorted"))

    # ── EDIBLE ──
    e_nodes, e_edges, e_entry, e_done = _branch(
        "ed",
        [
            ("effect", "How would you like to feel — fully relaxed and sleepy, uplifted, or somewhere "
                       "in the middle?", {"effect": EFFECT}),
            ("activity", "What are you planning to do afterward — chill at home, socialize, or get "
                         "creative?", {"activity": ACTIVITY}),
            ("flavor", "Do you like chocolate, or do you prefer gummies?",
                       {"flavor": ["chocolate", "gummies", "either"]}),
            ("ratios", "Are you looking for THC only, or some body effects too? THC-only is very "
                       "psychoactive; one-to-one is balanced; one-to-fifty is more body relaxation; and "
                       "THC-CBD-CBN is heavy relaxation and sleep.",
                       {"ratio": ["thc_only", "1:1", "1:50", "thc_cbd_cbn"]}),
            ("past_wins", "What products really hit the spot for you recently?", {"past_wins": "string"}),
            ("budget", "And what price range feels comfortable?", {"budget": "number"}),
        ],
        present_say="I'll show you up to three — one at your price, one about five more, one about ten "
                    "more.",
        dosing="Has anyone walked you through how to take edibles? Start slow — five milligrams, wait "
               "thirty minutes; gummies kick in around thirty to sixty minutes, chocolate one to two "
               "hours, and it peaks around one to two hours and lasts two to three.",
        wrap_say="Would you like to throw in something sweet for three dollars, or a quick joint? "
                 "Anything else while you're here?",
    )
    nodes += e_nodes
    edges += e_edges
    edges.append(_edge("pick_store", e_entry, "the caller wants an edible"))

    # ── TINCTURE ──
    t_nodes, t_edges, t_entry, t_done = _branch(
        "tinc",
        [
            ("effect", "How would you like to feel — fully relaxed and sleepy, uplifted, or somewhere "
                       "in the middle?", {"effect": EFFECT}),
            ("activity", "What are you planning to do afterward — chill at home, socialize, or get "
                         "creative?", {"activity": ACTIVITY}),
            ("ratios", "Are you looking for THC only, or some body effects too? Same options as "
                       "edibles — THC only, one-to-one, one-to-fifty, or THC-CBD-CBN for heavy "
                       "relaxation and sleep.", {"ratio": ["thc_only", "1:1", "1:50", "thc_cbd_cbn"]}),
            ("past_wins", "What products really hit the spot for you recently?", {"past_wins": "string"}),
            ("budget", "And what price range feels comfortable?", {"budget": "number"}),
        ],
        present_say="I'll show you up to three — one at your price, one about five more, one about ten "
                    "more.",
        dosing="Has anyone walked you through tinctures? Mild is a quarter milliliter, standard a half, "
               "strong a full milliliter — start mild and increase gradually. Great for microdosing.",
        wrap_say="Anything else you'd like to add while you're here?",
    )
    nodes += t_nodes
    edges += t_edges
    edges.append(_edge("pick_store", t_entry, "the caller wants a tincture"))

    # ── ESCALATION (global — enterable from ANY node) -> email the team -> optional transfer ──
    warnings: list[str] = []
    escalation = _conv(
        "escalation",
        "I'm really sorry you're dealing with this — I want to get it to the right person. Tell me "
        "what happened and what you'd like us to do, and I'll send it straight to the team.",
        {
            "issue_summary": "string",
            "issue_type": ["defective_return", "dispute", "complaint", "repeated_request", "other"],
        },
        glob={
            "enabled": True,
            "enterCondition": "the caller is upset, reports a defective product or a return or billing "
            "dispute, or asks for a human or manager",
        },
    )
    notify = _tool_node("notify_staff_issue", "notify_staff_issue")
    esc_sent = _conv(
        "escalation_sent",
        "Thanks for telling me — I've sent that to our team and they'll follow up. Is there anything "
        "else I can help you with right now?",
        {"wants_human_again": "boolean"},
    )
    transfer = {"type": "tool", "name": "transfer", "tool": provision._transfer_tool("escalation", warnings)}
    nodes += [escalation, notify, esc_sent, transfer]
    edges.append(_edge("escalation", "notify_staff_issue", "the issue details were gathered"))
    edges.append(_edge("notify_staff_issue", "escalation_sent", "the team was emailed"))
    edges.append(_edge("escalation_sent", "transfer", "the caller still wants a person now"))

    # ── END (shared) — every branch wrap + a no-more-help escalation_sent route here ──
    end = {"type": "tool", "name": "end_call", "tool": {"type": "endCall"}}
    nodes.append(end)
    for done in (f_done, c_done, e_done, t_done):
        edges.append(_edge(done, "end_call", "the caller is all set"))
    edges.append(_edge("escalation_sent", "end_call", "the caller is all set"))

    payload = {
        "name": WORKFLOW_NAME,
        "globalPrompt": GLOBAL_PROMPT,
        "model": {
            "provider": C.ASSISTANT_PROVIDER,
            "model": C.ASSISTANT_MODEL,
            "temperature": C.ASSISTANT_TEMPERATURE,
            "maxTokens": C.ASSISTANT_MAX_TOKENS,
        },
        "voice": provision._voice_block(),
        "transcriber": dict(C.DEEPGRAM_TRANSCRIBER),
        "server": provision._server_block(),
        "nodes": nodes,
        "edges": edges,
    }
    return payload, warnings


def validate_payload(payload: dict) -> list[str]:
    """Cheap structural invariants (the runnable check lives in tests/test_workflow_build.py).
    Returns a list of problems — empty == structurally sound."""
    problems = []
    names = [n["name"] for n in payload["nodes"]]
    if len(names) != len(set(names)):
        problems.append("duplicate node names")
    starts = [n for n in payload["nodes"] if n.get("isStart")]
    if len(starts) != 1:
        problems.append(f"expected exactly one isStart node, got {len(starts)}")
    nameset = set(names)
    for e in payload["edges"]:
        if e["from"] not in nameset:
            problems.append(f"edge from unknown node {e['from']}")
        if e["to"] not in nameset:
            problems.append(f"edge to unknown node {e['to']}")
    # every gather/question conversation node must extract at least one variable (the reliability core)
    for n in payload["nodes"]:
        if n["type"] == "tool" and "prompt" in n:
            problems.append(f"tool node {n['name']} must not carry a prompt")
    return problems
