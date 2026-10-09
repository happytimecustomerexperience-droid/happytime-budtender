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

# ── Model — the ONE intentional model (ADR-024 owner override of ADR-010's gpt-4.1-mini →
# Gemini 2.5 Flash), never the shadowed gpt-5.2-chat-latest. This is the single code source
# for the assistant model; kb/seed.py imports these two names rather than redefining them.
ASSISTANT_PROVIDER = "google"
ASSISTANT_MODEL = "gemini-2.5-flash"
ASSISTANT_TEMPERATURE = 0.3  # export per-node value (L17)
ASSISTANT_MAX_TOKENS = 250  # export per-node value (L18); router member can run 200

# ── serverMessages — webhook events voice/webhooks.py handles (§4.4) ──────────
# NOTE: "assistant-request" is NOT a valid Vapi serverMessage (rejected with 400);
# assistants here are pre-provisioned squad members, so it isn't needed.
SERVER_MESSAGES = ["tool-calls", "status-update", "end-of-call-report"]

# ── Per-member tool attachment (§4.2) ────────────────────────────────────────
# A member's resolved toolIds = [VapiObject(kind="tool", name=n).vapi_id for n in tool_names].
# A name without a provisioned id → that assistant is reported skipped (no dangling toolId).
MEMBER_TOOLS = {
    "entry_router": ["faq_lookup"],
    # faq_lookup too: callers ask about deals/hours/returns MID-pick — the budtender must be able to
    # answer from the KB without a handoff (else it says "I don't have access to the deals").
    "budtender": ["suggest_products", "check_inventory", "pair_upsell", "faq_lookup", "stage_phone_cart"],
    "faq": ["faq_lookup"],  # + the KB Query Tool (attached by ensure_files)
    "vendor": ["notify_vendor_callback"],  # + transferCall (one destination per store)
    "escalation": ["notify_staff_issue"],  # gather+email is the default; transferCall is last-resort
    # The single-mode front agent does all of the above itself (+ transferCall when call.transfer is on,
    # + remember_caller while HHT_DYNAMIC_GREETING is on).
    "concierge": [
        "faq_lookup", "suggest_products", "check_inventory", "pair_upsell", "stage_phone_cart",
        "notify_vendor_callback", "notify_staff_issue",
    ],
}

# P0 ships ONE merged member: entry_faq (entry + FAQ), AgentPrompt.role="faq" so the later
# faq split is a rename, not a new row (10-P0 §6.4). Its tools are the faq set.
P0_ASSISTANT_NAME = "entry_faq"
P0_ASSISTANT_ROLE = "faq"

# The category enum of suggest_products / check_inventory (see the lockstep note on the
# suggest_products ``category`` property below).
PRODUCT_CATEGORIES = (
    "flower", "concentrate", "cartridge", "edible", "tincture", "pre-roll",
    "topical", "capsule", "mint", "blunt", "infused-blunt",
)

# The price gate (voice/tools/suggest.py): a price is per SIZE, so a search in one of these categories
# that carries no ``size`` slot gets NO price in its result — it asks the size instead. Every other
# category in the enum has no size concept and is exempt. A category that is blank or not in the enum
# is NOT exempt: unknown fails closed.
SIZE_REQUIRED_CATEGORIES = frozenset(
    {"flower", "concentrate", "cartridge", "edible", "tincture", "pre-roll"}
)
SIZE_EXEMPT_CATEGORIES = frozenset(PRODUCT_CATEGORIES) - SIZE_REQUIRED_CATEGORIES
# budtender's _size_match treats these as "no opinion" (they filter nothing) — never a real size.
NO_SIZE_VALUES = frozenset({"", "any", "stock-up", "disposable"})

# The five scents the questionnaire offers (the ``aroma`` slot). Mirrors the keys of budtender's
# ``terpenes.AROMA_TERPENES`` (the one aroma map); budtender ignores any other value.
AROMAS = ("citrus", "earthy", "pine", "floral", "spicy")
# The light, skippable scent question — one string for the phone prompt (kb/seed.py) and the text brain
# (chat.py, which also spots it on the agent's last line to read the caller's answer as the aroma slot).
AROMA_QUESTION = f"Any scent you're drawn to — {', '.join(AROMAS[:-1])}, or {AROMAS[-1]}?"

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
                # Retrieval is tuned to how callers actually phrase things; a model-written
                # abstraction ("ID requirements") misses rows the caller's own words hit.
                "query": {
                    "type": "string",
                    "description": "The caller's question in their OWN words, verbatim — never a "
                    "rephrased or shortened summary.",
                },
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                # Constrains retrieval to the subject the caller actually asked about (enum-only —
                # _sanitize_args drops any value outside it). "" = unconstrained, today's behaviour.
                "topic": {
                    "type": "string",
                    # No "" member: Vertex rejects an empty enum value ("enum[3]: cannot be
                    # empty") and the whole tool declaration with it. Omitting ``topic`` is
                    # how "unconstrained" is expressed; faq_lookup derives it from the words.
                    "enum": ["hours_location", "specials", "return_policy"],
                },
            },
            "required": ["query"],
        },
        "async": False,
    },
    "suggest_products": {
        "description": (
            "Return up to 3 in-stock, leak-safe product picks for the caller's slots, each "
            "with a speakable why_this and — once a size is given — an out-the-door price "
            "(price_otd / price_spoken). With NO size slot the picks carry no price at all: the "
            "result is needs_size:true with size_options and a spoken_summary that asks the size. "
            "NEVER returns cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "category": {
                    "type": "string",
                    # "pre-roll" was missing while chat.py derived it, so _sanitize_args dropped
                    # the category and the handler then failed its own required-field check —
                    # every pre-roll ask answered "nothing in stock". budtender maps it in
                    # CATEGORY_BY_SLOTKEY.
                    # 2026-08-10: topical/capsule/mint/blunt/infused-blunt added — live Dutchie
                    # inventory has these categories in stock, but they were absent from this
                    # enum, so even a chat.py that correctly derived the category had it dropped
                    # right here by _sanitize_args. Keep this list in lockstep with
                    # budtender/ranking.py CATEGORY_BY_SLOTKEY's keys (test_category_drift_alarm.py
                    # in the budtender repo asserts it).
                    "enum": list(PRODUCT_CATEGORIES),
                },
                "subcategory": {"type": "string"},
                # A caller who names a brand and no category ("you guys still carrying Phat
                # Panda") is making a real product request; without a brand slot the name was
                # simply dropped by _sanitize_args and the search ran blind. budtender ranks on
                # brand itself.
                "brand": {"type": "string"},
                # THE gate on every price (suggest.py): a size-required category searched with NO size
                # carries no price at all. A price_max ceiling alone does not stand in for it.
                "size": {"type": "string"},
                # Did the caller ask a price ("how much", "what do they run")? With no size, only then is
                # the answer the size question; otherwise the picks are named without a price (as chat).
                "asked_price": {
                    "type": "boolean",
                    "description": "true when the caller asked a price or how much something costs.",
                },
                "price_tier": {"type": "string", "enum": ["value", "mid", "top"]},
                "price_min": {"type": "number"},
                "price_max": {"type": "number"},
                "effect_desired": {"type": "string", "enum": ["relaxed", "uplifted", "middle"]},
                # The scent the caller is drawn to; omit it for "no preference". budtender nudges the
                # ranking toward real batch-lab terpenes (its terpenes.AROMA_TERPENES keys) and
                # ignores anything else — _sanitize_args drops a value outside this enum the same way.
                "aroma": {"type": "string", "enum": list(AROMAS)},
                "doh_only": {"type": "boolean"},
                # "something stronger" / "something cheaper": the SAME slots again plus this, so the
                # caller's budget, size and effect are kept and budtender only re-orders (Contract
                # B). Without the slot the ask lost everything but the category — and _sanitize_args
                # would silently drop any value not declared here.
                "sort_by": {
                    "type": "string",
                    "enum": ["potency", "price_asc"],
                    "description": "potency = highest THC first (the caller wants something "
                    "stronger); price_asc = cheapest first. Keep every other slot the caller gave.",
                },
                # "something different": the SKUs already offered. Never for "stronger"/"cheaper" —
                # excluding them can hide the strongest or cheapest pick if it was already shown.
                "exclude_skus": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "SKUs already offered, to leave out when the caller asks for "
                    "something different.",
                },
            },
            "required": ["store", "category"],
        },
        "async": False,
    },
    "check_inventory": {
        "description": (
            "Check whether a SKU is purchasable at a store. Returns {in_stock, qty_band} plus "
            "price_otd / price_spoken ONLY when you pass the size the caller chose (pass the "
            "category too — a category with no size concept needs none); without a size the price "
            "is withheld and the result is needs_size:true. NEVER cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "sku": {"type": "string"},
                # The same size gate as suggest_products. budtender's by-sku row carries no category,
                # so the caller says it: only a category with no size concept (topical, mint…) is priced
                # without a size; a blank category with no size fails closed (price withheld).
                "category": {"type": "string", "enum": list(PRODUCT_CATEGORIES)},
                "size": {"type": "string"},
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
    "stage_phone_cart": {
        "description": (
            "Stage or release a phone-cart draft for POS staff. This never submits, reserves, "
            "or writes a Dutchie order; it only prepares a register handoff."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add_item", "remove_item", "set_quantity", "quote", "release"],
                },
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
                "sku": {"type": "string"},
                "quantity": {"type": "number"},
                "draft_token": {"type": "string"},
                "call_id": {"type": "string"},
                "session_token": {"type": "string"},
                "pickup_name": {"type": "string"},
            },
            "required": ["action", "store"],
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
        # Synchronous: the agent must hear the tool's own `spoken` (FOLLOWUP_NOT_CONFIRMED when the
        # alert did not go out). With async Vapi does not wait, so the agent promised a callback blind.
        "async": False,
    },
    "notify_n8n": {
        "description": (
            "Trigger an n8n automation workflow for a follow-up the caller agreed to — e.g. text "
            "me the online-menu link, add me to the deals list, or log a callback request. "
            "Fire-and-forget: it queues the action and returns an acknowledgement. Use ONLY for an "
            "action the caller asked for. NEVER returns cost or margin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_type": {
                    "type": "string",
                    "description": (
                        "A short action key for the n8n workflow to switch on, e.g. "
                        "'send_menu_link', 'deals_signup', 'callback_request'."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "One sentence on what the caller wants. No phone number.",
                },
                "store": {"type": "string", "enum": ["yakima", "mount-vernon", "pullman"]},
            },
            "required": ["event_type"],
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
                        # A caller asking to be told when something is back in stock: no waitlist
                        # tool exists, so the request is filed for a person to action.
                        "restock_request",
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
        "async": False,  # same reason as notify_vendor_callback: speak what really happened
    },
    "remember_caller": {
        "description": (
            "Save the caller's FIRST NAME on their profile once they have told you what to call "
            "them. Use it only after the CALLER line says their name is unknown and you asked once. "
            "Pass just the first name. Returns only {saved}; say nothing about it either way."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "first_name": {
                    "type": "string",
                    "description": "The caller's first name only: one word, letters only, at most 30.",
                },
            },
            "required": ["first_name"],
        },
        "async": False,
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

# ── Squad mode (settings.HHT_SQUAD_MODE) ─────────────────────────────────────────
# "single" (default): ONE front agent, role ``concierge``, in a one-member squad (so the owner's squad
# id stays the one in use) with NO assistantDestinations: the caller never hears a handoff. "multi":
# the SQUAD_SHAPE above, unchanged. Single mode applies once the concierge assistant exists; until
# provision_vapi has created it, every builder answers exactly as multi mode (the live line keeps
# working through the deploy).
CONCIERGE_ROLE = "concierge"
SQUAD_MODES = ("single", "multi")
DEFAULT_SQUAD_MODE = "single"
MULTI_SQUAD_SHAPE = SQUAD_SHAPE
SINGLE_SQUAD_SHAPE = {CONCIERGE_ROLE: []}
_ENTRY_ROLE = {"single": CONCIERGE_ROLE, "multi": "entry_router"}
# The roles that open a call with the fixed greeting (firstMessage) and carry the CALLER line rules.
ENTRY_ROLES = ("entry_router", CONCIERGE_ROLE)
# The roles that carry the built-in transferCall (while call.transfer is on).
TRANSFER_ROLES = ("vendor", "escalation", CONCIERGE_ROLE)


def squad_mode() -> str:
    """``settings.HHT_SQUAD_MODE`` as ``single`` | ``multi`` (anything else reads as the default)."""
    from django.conf import settings

    mode = str(getattr(settings, "HHT_SQUAD_MODE", DEFAULT_SQUAD_MODE) or "").strip().lower()
    return mode if mode in SQUAD_MODES else DEFAULT_SQUAD_MODE


def squad_shape(mode: str | None = None) -> dict:
    """The code-defined topology for ``mode`` (default: the configured mode)."""
    return SINGLE_SQUAD_SHAPE if (mode or squad_mode()) == "single" else MULTI_SQUAD_SHAPE


def entry_role(mode: str | None = None) -> str:
    """The role that answers the call in ``mode``: ``concierge`` (single) or ``entry_router`` (multi)."""
    return _ENTRY_ROLE[mode or squad_mode()]

# Spoken store names — tools voice "Mount Vernon", never the raw slug ("mount-vernon"/"mt_vernon").
STORE_SPOKEN = {
    "yakima": "Yakima",
    "mount-vernon": "Mount Vernon",
    "mt_vernon": "Mount Vernon",
    "pullman": "Pullman",
}


def spoken_store(store: str) -> str:
    """The readable store name for a slug, so a tool's spoken envelope never voices a code."""
    key = (store or "").strip().lower()
    return STORE_SPOKEN.get(key, key.replace("_", " ").replace("-", " ").title()) or "your"


# The three stores a warm transfer can reach: (settings key, store slug). The number for a store is
# settings.HHT_TRANSFER_NUMBER_<KEY> (env, O-4); the spoken name comes from ``spoken_store(slug)``.
TRANSFER_STORES = (
    ("YAKIMA", "yakima"),
    ("MTVERNON", "mount-vernon"),
    ("PULLMAN", "pullman"),
)
# Documented placeholder when a transfer number is unset (O-4) — never blocks the run.
TRANSFER_NUMBER_PLACEHOLDER = "+10000000000"
