"""Mid-call back-edge / correction handling (P5, export weakness #12; 15-P5 §3.3).

The legacy export was strictly-forward: a caller could not revise a prior choice mid-flow — "actually
make it edibles" marched on against the stale flower graph. This module makes the conversation
editable IN-FLIGHT. It is CODE-OWNED (the swedish-bot discipline: code owns the slot FSM, the LLM
only fills/classifies). The budtender member emits a structured ``correction`` signal when the caller
revises; the server reads it here, resets the affected slots deterministically, and the member
resumes slot-filling from the corrected state.

Two pure functions (deterministic, no network, unit-testable):
  * ``detect_correction(prev_slots, new_user_intent) -> CorrectionPlan | None`` — recognizes a
    revision ("actually / wait / no, make it / change to / instead") + a new category/effect/budget/
    size, returning which slots to CLEAR and which to REWRITE.
  * ``apply_correction(slot_state, plan) -> slot_state`` — applies the plan: a CATEGORY change clears
    the downstream category-specific slots (subcategory/size/strain_type/price_tier — they don't
    transfer across categories) and PRESERVES the category-agnostic ones (effect_desired/budget/
    store/phone_hash); a single-slot change overwrites just that slot.

Binding (15-P5 §4.3): the corrected state persists in the budtender session / VoiceCall, NEVER in
process memory (stateless-turn discipline) — that is the caller's responsibility, not this module's;
these functions are pure. Idempotent: applying a plan twice == once.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Correction "kinds" — what the caller is revising.
KIND_CATEGORY = "category"
KIND_EFFECT = "effect"
KIND_BUDGET = "budget"
KIND_SIZE = "size"
KIND_CANCEL = "cancel"

# The category-specific slots that DON'T transfer when the category changes (flower→edible can't
# keep a flower size or strain_type). Cleared on a KIND_CATEGORY correction.
_CATEGORY_DEPENDENT_SLOTS = ("subcategory", "size", "strain_type", "price_tier")
# Category-agnostic slots that survive a category change (the caller still wants the same effect /
# budget / store). NEVER cleared by a category correction.
_CATEGORY_AGNOSTIC_SLOTS = (
    "effect_desired",
    "budget",
    "price_min",
    "price_max",
    "store",
    "phone_hash",
)

# budtender's category enum (15-P5 §3.2 / §4.2). A correction's category must land in here.
_CATEGORIES = {"flower", "concentrate", "cartridge", "edible", "tincture"}

# Cartridge aliases (mirror voice/routing) → canonical 'cartridge'. A "make it a cart" correction
# must NEVER resolve to 'concentrate'.
_CARTRIDGE_ALIASES = {
    "cart",
    "carts",
    "cartridge",
    "cartridges",
    "510",
    "vape",
    "vapes",
    "vape pen",
    "vape pens",
    "disposable",
    "disposables",
    "dispo",
    "aio",
    "all-in-one",
    "pod",
    "pods",
}
_CATEGORY_LEXICON = {
    "edible": "edible",
    "edibles": "edible",
    "gummy": "edible",
    "gummies": "edible",
    "flower": "flower",
    "bud": "flower",
    "eighth": "flower",
    "pre-roll": "flower",
    "preroll": "flower",
    "concentrate": "concentrate",
    "dab": "concentrate",
    "wax": "concentrate",
    "rosin": "concentrate",
    "shatter": "concentrate",
    "tincture": "tincture",
    "tinctures": "tincture",
    "drops": "tincture",
}

# The revision trigger — "actually / wait / no, make it / change to / instead / on second thought".
_REVISION = re.compile(
    r"\b(actually|wait|never\s*mind|on\s+second\s+thought|change\s+(it\s+|that\s+|my\s+)?to|"
    r"make\s+(it|that)|instead|scratch\s+that|i\s+changed\s+my\s+mind|no,?\s+(make|let'?s|i\s+want)|"
    r"can\s+(you|we)\s+(do|make\s+it)|switch\s+to|rather\s+have)\b",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"\b(cancel\s+(that|it|this)|start\s+over|forget\s+(it|that|the)|never\s*mind\s+all)\b",
    re.IGNORECASE,
)
_EFFECT_LEXICON = {
    "relaxed": (
        "sleep",
        "sleepy",
        "relax",
        "relaxed",
        "relaxing",
        "calm",
        "calming",
        "chill",
        "body",
        "couch",
        "nightcap",
    ),
    "uplifted": (
        "energy",
        "energetic",
        "uplift",
        "uplifted",
        "uplifting",
        "focus",
        "social",
        "up",
        "creative",
        "daytime",
    ),
    "balanced": ("balanced", "middle", "even", "mellow"),
}
_BUDGET = re.compile(
    r"(?:under|below|less\s+than|up\s+to|max(?:imum)?|around|about|"
    r"budget\s+(?:of\s+|is\s+|to\s+)?)\$?\s*(\d{1,4})",
    re.IGNORECASE,
)
_SIZE = re.compile(
    r"\b(0\.5\s*g|\.5\s*g|half\s*gram|1\s*g|one\s*gram|full\s*gram|3\.5\s*g|eighth|7\s*g|quarter|"
    r"14\s*g|half\s*ounce|28\s*g|ounce|10\s*mg|5\s*mg|2\.5\s*mg|100\s*mg)\b",
    re.IGNORECASE,
)


@dataclass
class CorrectionPlan:
    """A deterministic plan: the ``kind`` of revision, the new ``to`` value, the slots to ``clear``
    and to ``keep``, and the raw caller phrase (for the log). Built by ``detect_correction``."""

    kind: str
    to: str | None = None
    clear: list[str] = field(default_factory=list)
    keep: list[str] = field(default_factory=list)
    raw: str = ""


def _resolve_category(text: str) -> str | None:
    """The first category named in ``text`` (cartridge aliases → 'cartridge', never 'concentrate')."""
    lowered = text.lower()
    # cartridge first (so "make it a vape cart" → cartridge, outranking the concentrate lexicon).
    for alias in _CARTRIDGE_ALIASES:
        if re.search(rf"\b{re.escape(alias)}\b", lowered):
            return "cartridge"
    for token, cat in _CATEGORY_LEXICON.items():
        if re.search(rf"\b{re.escape(token)}\b", lowered):
            return cat
    return None


def _resolve_effect(text: str) -> str | None:
    lowered = text.lower()
    for effect, words in _EFFECT_LEXICON.items():
        if any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in words):
            return effect
    return None


def detect_correction(prev_slots: dict, new_user_intent: str) -> CorrectionPlan | None:
    """Detect a mid-flow correction in ``new_user_intent`` against the ``prev_slots`` already filled.

    Returns a ``CorrectionPlan`` or ``None`` (no revision detected → the flow continues normally).
    Precedence: cancel/start-over → category → effect → budget → size. A category change is the only
    kind that clears downstream slots; the others overwrite just their own slot. Requires a revision
    trigger ("actually / make it / instead / no, …") so a plain new statement isn't mistaken for a
    correction — except an explicit cancel, which stands alone."""
    text = (new_user_intent or "").strip()
    if not text:
        return None

    if _CANCEL.search(text):
        # Reset to the category-entry stage: clear everything category-dependent + the category.
        clear = ["category", *_CATEGORY_DEPENDENT_SLOTS]
        return CorrectionPlan(kind=KIND_CANCEL, to=None, clear=clear, keep=[], raw=text)

    if not _REVISION.search(text):
        return None  # a non-revision statement is normal slot-filling, not a correction.

    # 1) category change — clears the downstream category-dependent slots, keeps the agnostic ones.
    new_cat = _resolve_category(text)
    prev_cat = (prev_slots or {}).get("category")
    if new_cat and new_cat != prev_cat:
        keep = [s for s in _CATEGORY_AGNOSTIC_SLOTS if s in (prev_slots or {})]
        return CorrectionPlan(
            kind=KIND_CATEGORY,
            to=new_cat,
            clear=list(_CATEGORY_DEPENDENT_SLOTS),
            keep=keep,
            raw=text,
        )

    # 2) effect change — overwrite just effect_desired.
    new_effect = _resolve_effect(text)
    if new_effect and new_effect != (prev_slots or {}).get("effect_desired"):
        return CorrectionPlan(kind=KIND_EFFECT, to=new_effect, clear=[], keep=[], raw=text)

    # 3) budget change — overwrite price_max.
    budget = _BUDGET.search(text)
    if budget:
        return CorrectionPlan(kind=KIND_BUDGET, to=budget.group(1), clear=[], keep=[], raw=text)

    # 4) size change — overwrite size.
    size = _SIZE.search(text)
    if size:
        return CorrectionPlan(kind=KIND_SIZE, to=size.group(1).strip(), clear=[], keep=[], raw=text)

    return None


def apply_correction(slot_state: dict, plan: CorrectionPlan | None) -> dict:
    """Apply a ``CorrectionPlan`` to a slot dict, returning a NEW dict (pure — never mutates input).

    * ``KIND_CATEGORY`` → set the new ``category``, CLEAR the category-dependent slots
      (subcategory/size/strain_type/price_tier), PRESERVE the category-agnostic ones.
    * ``KIND_EFFECT`` → overwrite ``effect_desired``.
    * ``KIND_BUDGET`` → overwrite ``price_max`` (int).
    * ``KIND_SIZE``  → overwrite ``size``.
    * ``KIND_CANCEL`` → clear the category + all category-dependent slots (back to category-entry).

    Idempotent: applying the same plan twice == once."""
    state = dict(slot_state or {})
    if plan is None:
        return state

    for slot in plan.clear:
        state.pop(slot, None)

    if plan.kind == KIND_CATEGORY and plan.to:
        state["category"] = plan.to
    elif plan.kind == KIND_EFFECT and plan.to:
        state["effect_desired"] = plan.to
    elif plan.kind == KIND_BUDGET and plan.to:
        try:
            state["price_max"] = int(plan.to)
        except (TypeError, ValueError):
            pass
    elif plan.kind == KIND_SIZE and plan.to:
        state["size"] = plan.to
    # KIND_CANCEL only clears (handled by the loop above) — back to the category-entry stage.
    return state


def correction_from_signal(signal: dict, prev_slots: dict) -> CorrectionPlan | None:
    """Build a ``CorrectionPlan`` from the budtender member's structured ``correction`` signal
    (§4.3): ``{"kind","to","raw"}``. The member already classified the revision; the server owns the
    slot transition (clear/keep) deterministically here. A category ``to`` is canonicalized (cartridge
    aliases → 'cartridge'). An unknown/empty kind → ``None`` (treated as no correction)."""
    if not isinstance(signal, dict):
        return None
    kind = (signal.get("kind") or "").strip().lower()
    to = signal.get("to")
    raw = signal.get("raw") or ""
    if kind == KIND_CATEGORY:
        cat = _canonical_category(to)
        if not cat:
            return None
        keep = [s for s in _CATEGORY_AGNOSTIC_SLOTS if s in (prev_slots or {})]
        return CorrectionPlan(
            kind=KIND_CATEGORY,
            to=cat,
            clear=list(_CATEGORY_DEPENDENT_SLOTS),
            keep=keep,
            raw=raw,
        )
    if kind == KIND_CANCEL:
        return CorrectionPlan(
            kind=KIND_CANCEL,
            to=None,
            clear=["category", *_CATEGORY_DEPENDENT_SLOTS],
            keep=[],
            raw=raw,
        )
    if kind in (KIND_EFFECT, KIND_BUDGET, KIND_SIZE) and to not in (None, ""):
        return CorrectionPlan(kind=kind, to=str(to), clear=[], keep=[], raw=raw)
    return None


def _canonical_category(value) -> str | None:
    """Canonicalize a category value: cartridge aliases → 'cartridge'; a known enum value passes
    through; anything else → None (don't fabricate a category)."""
    raw = str(value or "").strip().lower()
    if raw in _CARTRIDGE_ALIASES:
        return "cartridge"
    if raw in _CATEGORIES:
        return raw
    return _resolve_category(raw)
