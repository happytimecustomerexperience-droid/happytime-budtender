"""Member-level Vapi config — set ONCE per assistant, never per node (ADR-011).

The single source of truth for the shared voice/transcriber/model blocks +the per-member
tool attachment + the code-defined Squad topology. ``voice/provision.py`` reads ONLY from
here so a payload shape is fixed in one place (20-SPEC-vapi-deploy.md §4.1/§4.2/§4.7).

The Cartesia "Koptza" voice block + the Deepgram nova-3 33-term keyterm list are lifted from
the Vapi export (``happy-time-voice-agent-(full-script)-(uploaded-via-json).json`` L21–72).
The export duplicated voice/transcriber/model 51× per node (bug #7); these constants are
emitted exactly once per assistant — a unit test pins "appears once."
"""

from __future__ import annotations

# ── Cartesia sonic-3 "Koptza" (export L21–31; voiceId verbatim, ADR-011) ──────
# voiceId is overridable via settings.VAPI_VOICE_ID; the constant carries the default.
CARTESIA_VOICE = {
    "provider": "cartesia",
    "voiceId": "a3520a8f-226a-428d-9fcd-b0a4711a6829",
    "model": "sonic-3",
    "language": "en",
    "experimentalControls": {"emotion": ["positivity:highest"]},
}

# ── Deepgram nova-3 + the EXACT 33-term cannabis keyterm boost list (export L32–72) ──
# ONE shared constant; appears exactly once per assistant (no per-node dup — export bug #7).
# The export's "all‑in‑one" used a non-breaking hyphen (U+2011); normalized to a plain "-"
# so the keyterm matches transcripts (the one deliberate normalization of the lifted list).
DEEPGRAM_KEYTERMS = [
    "flower",
    "bud",
    "pre-roll",
    "pre-rolls",
    "joint",
    "joints",
    "concentrate",
    "concentrates",
    "dabs",
    "wax",
    "shatter",
    "resin",
    "live resin",
    "rosin",
    "cartridge",
    "cartridges",
    "cart",
    "carts",
    "vape",
    "vapes",
    "vape pen",
    "510",
    "disposable",
    "all-in-one",
    "edible",
    "edibles",
    "gummies",
    "chocolate",
    "drink",
    "drinks",
    "tincture",
    "tinctures",
    "oil",
]  # fmt: skip — 33 terms; the count is asserted in tests.

DEEPGRAM_TRANSCRIBER = {
    "provider": "deepgram",
    "model": "nova-3",
    "numerals": True,  # export L70 — spoken digits transcribed as numerals
    "keyterm": DEEPGRAM_KEYTERMS,  # Vapi/Deepgram field name "keyterm" (the export uses "keyterm")
}

# ── Model — the ONE intentional model (ADR-010), never the shadowed gpt-5.2-chat-latest ──
ASSISTANT_PROVIDER = "openai"
ASSISTANT_MODEL = "gpt-4.1-mini"
ASSISTANT_TEMPERATURE = 0.3  # export per-node value (L17)
ASSISTANT_MAX_TOKENS = 250  # export per-node value (L18); router member can run 200

# The fixed opener the entry member speaks first (firstMessageMode=assistant-speaks-first). Set as
# `firstMessage` on the entry_router payload so every call opens with the Happy Time greeting
# (deterministic — no model variance) and we DON'T ask the store until it matters.
ENTRY_FIRST_MESSAGE = (
    "Welcome to Happy Time! I can help you pick out flower, carts, edibles, concentrates, or "
    "tinctures, answer questions about our hours, deals, payment, or returns, or get you over to "
    "the team — what can I do for you today?"
)

# ── serverMessages — webhook events voice/webhooks.py handles (§4.4) ──────────
# NOTE: "assistant-request" is NOT a valid Vapi serverMessage (rejected with 400);
# assistants here are pre-provisioned squad members, so it isn't needed.
SERVER_MESSAGES = ["tool-calls", "status-update", "end-of-call-report"]

# ── Per-member tool attachment (§4.2) ────────────────────────────────────────
# A member's resolved toolIds = [VapiObject(kind="tool", name=n).vapi_id for n in tool_names].
# A name without a provisioned id → that assistant is reported skipped (no dangling toolId).
MEMBER_TOOLS = {
    "entry_router": ["faq_lookup"],
    "budtender": ["suggest_products", "check_inventory", "pair_upsell"],
    "faq": ["faq_lookup"],  # + the KB Query Tool (attached by ensure_files)
    "vendor": ["notify_vendor_callback"],  # + transferCall (built from transfer_number_key)
    "escalation": ["notify_staff_issue"],  # gather+email is the default; transferCall is last-resort
}

# P0 ships ONE merged member: entry_faq (entry + FAQ), AgentPrompt.role="faq" so the later
# faq split is a rename, not a new row (10-P0 §6.4). Its tools are the faq set.
P0_ASSISTANT_NAME = "entry_faq"
P0_ASSISTANT_ROLE = "faq"

# ── Custom-tool JSON-Schema parameters (§4.5) — name → tool spec ──────────────
# Each is provisioned as a Vapi `function` tool whose server.url is our webhook; the webhook
# routes by function.name via TOOL_REGISTRY (ADR-020). P0 only ships faq_lookup; the others are
# declared so later phases (P1/P3) reconcile them without re-specifying the shape here.
TOOL_SPECS = {
    "faq_lookup": {
        "description": (
            "Answer hours/specials/returns/payment/pickup/limits/weights-types from the "
            "knowledge base. Returns grounded KB text only — never composes a figure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
            },
            "required": ["query"],
        },
        "async": False,
    },
    "suggest_products": {
        "description": (
            "Return up to 3 in-stock, leak-safe product picks for the caller's slots, each "
            "with a speakable why_this and an out-the-door price. NEVER returns cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "category": {
                    "type": "string",
                    "enum": ["flower", "concentrate", "cartridge", "edible", "tincture"],
                },
                "subcategory": {"type": "string"},
                "size": {"type": "string"},
                "price_tier": {"type": "string", "enum": ["value", "mid", "top"]},
                "price_min": {"type": "number"},
                "price_max": {"type": "number"},
                "effect_desired": {"type": "string", "enum": ["relaxed", "uplifted", "middle"]},
                "doh_only": {"type": "boolean"},
            },
            "required": ["store", "category"],
        },
        "async": False,
    },
    "check_inventory": {
        "description": (
            "Check whether a SKU is purchasable at a store. Returns "
            "{in_stock, qty_band, price_otd} — NEVER cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "sku": {"type": "string"},
            },
            "required": ["store", "sku"],
        },
        "async": False,
    },
    "pair_upsell": {
        "description": (
            "Return ONE complement for an anchor SKU, surfaced only if its strength clears the "
            "gate. Leak-safe — NEVER returns cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "anchor_sku": {"type": "string"},
                "session_token": {"type": "string"},
            },
            "required": ["store", "anchor_sku"],
        },
        "async": False,
    },
    "notify_vendor_callback": {
        "description": (
            "Log a vendor/wholesale/delivery/manifest callback after a no-answer transfer, alert "
            "store staff, and return the callback window to state to the caller. Async (the vendor "
            "flow, ADR-015). NEVER returns cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "reason": {
                    "type": "string",
                    "enum": [
                        "delivery",
                        "wholesale_order",
                        "manifest",
                        "sample_drop",
                        "invoice",
                        "other",
                    ],
                },
                "summary": {
                    "type": "string",
                    "description": "What the vendor is calling about, in one sentence.",
                },
                "caller_name": {
                    "type": "string",
                    "description": "Name/company the caller gives. No phone number.",
                },
            },
            "required": ["store", "reason", "summary"],
        },
        "async": True,
    },
    "notify_staff_issue": {
        "description": (
            "After you have LISTENED and gathered the caller's full issue (a complaint, a defective "
            "product, a billing/return dispute, or a repeated request for a person), log it and "
            "EMAIL the store team right away so they can follow up. Call this ONCE you have the "
            "details — it is the default path and replaces an immediate transfer. NEVER returns "
            "cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "issue_type": {
                    "type": "string",
                    "enum": [
                        "defective_return",
                        "dispute",
                        "complaint",
                        "repeated_request",
                        "other",
                    ],
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "The COMPLETE issue in the caller's words — what happened, which product or "
                        "order, what's wrong, and what they'd like done."
                    ),
                },
                "caller_name": {
                    "type": "string",
                    "description": "Name + best callback contact the caller gives. No raw phone number stored.",
                },
            },
            "required": ["store", "summary"],
        },
        "async": True,
    },
}

# ── Code-defined Squad topology (§4.7 / 01-ARCHITECTURE §1.6) ─────────────────
# The destinations come from code, never freely from the canvas. escalation has REAL inbound
# edges (the export's orphan, fixed by construction) and is terminal (warm transferCall out).
SQUAD_NAME = "Happy Time Voice"
SQUAD_SHAPE = {
    "entry_router": [
        ("budtender", "retail intent — looking for / recommend / what's good for…"),
        ("faq", "info intent — hours / specials / returns / payment / pickup / location"),
        ("vendor", "vendor / wholesale / delivery / manifest / dropping off"),
        ("escalation", ">=2 human requests OR return dispute OR defective product"),
    ],
    "budtender": [("escalation", "human request mid-flow")],
    "faq": [("budtender", "cross-sell"), ("escalation", "dispute / human request")],
    "vendor": [("escalation", "dispute / human request")],
    "escalation": [],  # terminal; warm transferCall out
}

# Per-member transfer destination key → settings.HHT_TRANSFER_NUMBER_<KEY> (env, O-4).
MEMBER_TRANSFER_KEY = {
    "vendor": "YAKIMA",
    "escalation": "YAKIMA",
}
# Documented placeholder when a transfer number is unset (O-4) — never blocks the run.
TRANSFER_NUMBER_PLACEHOLDER = "+10000000000"
