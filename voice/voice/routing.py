"""entry_router intent classification — the deterministic precedence matrix (13-P3 §4.1).

The Squad's live routing is one gpt-4.1-mini turn on the ``entry_router`` member (it emits the
``{intent, store, …}`` structured object and the squad fires the handoff). This module is the
CODE-OWNED mirror of that contract: a deterministic lexicon classifier that pins the precedence
(escalation > VENDOR > faq > retail) so the matrix is unit-testable WITHOUT a live LLM, and so the
fix for export weakness #6 (a vendor opener must never fall into the retail budtender slot-fill) is
a tested invariant, not just prompt copy.

``classify_intent(opener, human_requested=0)`` returns one of ``escalation | vendor | faq |
retail``, applying the §4.1 precedence:

  1. dispute / defective / "broken cart" / refund, OR human_requested >= 2 → escalation
  2. a vendor-lexicon hit                                                  → vendor   (before retail!)
  3. an info ask (hours/specials/returns/payment/pickup/location/limits)   → faq
  4. a retail-buyer ask (looking for / recommend / category / budget)      → retail   (→ budtender)
  5. ambiguous / none of the above                                         → faq      (safe default)

The handoff target for each intent is the Squad member name (``classify_destination``): vendor
intent → the ``vendor`` member (P3), retail → ``budtender`` (P1), faq → ``faq`` (P0), escalation →
``escalation`` (P2). The live model only fills the slot; the precedence + lexicon documented here
is the authoritative contract the prompt teaches by few-shots.
"""

from __future__ import annotations

import re

INTENT_ESCALATION = "escalation"
INTENT_VENDOR = "vendor"
INTENT_FAQ = "faq"
INTENT_RETAIL = "retail"

# intent → the Squad member the handoff targets (assistant name).
_INTENT_DESTINATION = {
    INTENT_ESCALATION: "escalation",
    INTENT_VENDOR: "vendor",
    INTENT_FAQ: "faq",
    INTENT_RETAIL: "budtender",
}

# ── 1) dispute / defective — escalation (precedence 1; OUTRANKS vendor so a HOSTILE vendor reaches
#      a human, not the callback loop — §4.1 A2). Tuned to the claim, not a mere mention.
_DISPUTE = re.compile(
    r"\b(defect\w*|broken\s+(cart|cartridge|pen|vape|battery)|won'?t\s+(fire|hit|charge|work)|"
    r"doesn'?t\s+(fire|hit|work)|malfunction\w*|refund|charged?\s+me\s+(twice|double)|overcharged|"
    r"short(ed|change)?\s+me|wrong\s+(order|item|change)|i\s+want\s+(my\s+)?money\s+back|dispute|"
    r"messed\s+up\s+my\s+order|complaint)\b",
    re.IGNORECASE,
)

# ── 2) vendor lexicon — checked BEFORE retail (the export-#6 fix). Few-shot vocab mirrors the
#      owner's real returns/auto-return/manifest workflows (01-ARCHITECTURE §1.4).
_VENDOR = re.compile(
    r"\b(vendor|wholesale|distributor|supplier|sales\s+rep|\brep\b|delivery|deliver\w*|"
    r"drop(ping)?[\s-]?off|drop\s+off|manifest|transfer\s+manifest|metrc|ccrs|wcia|"
    r"sample\s+drop|samples?|\bp\.?o\.?\b|purchase\s+order|invoice|accounts?\s+payable|"
    r"for\s+the\s+buyer|for\s+receiving|i'?m\s+the\s+driver|i'?m\s+here\s+with\s+an?\s+order|"
    r"here'?s\s+a\s+manifest|pallet)\b",
    re.IGNORECASE,
)

# ── 3) info / FAQ ask — hours/specials/returns/payment/pickup/location/limits/weights.
_FAQ = re.compile(
    r"\b(hours?|when\s+(do\s+you\s+)?(open|close)|what\s+time|special|deal|sale|return\s+policy|"
    r"can\s+i\s+return|payment|pay|cash|debit|card|atm|pickup|pick\s+up|delivery\?|do\s+you\s+"
    r"deliver|where\s+(are\s+you|is\s+the\s+store)|location|address|directions|limit|how\s+much\s+"
    r"can\s+i\s+buy|how\s+many\s+grams|ounce)\b",
    re.IGNORECASE,
)

# ── 4) retail-buyer ask — looking for / recommend / a category/effect/budget.
_RETAIL = re.compile(
    r"\b(looking\s+for|recommend|suggest|what'?s\s+good|something\s+for|help\s+me\s+(find|sleep|"
    r"relax)|i\s+want\s+(a|an|some)|i\s+need\s+(a|an|some)|under\s+\$?\d+|flower|edible|gumm\w+|"
    r"cart(ridge)?|vape|pre-?roll|concentrate|tincture|indica|sativa|hybrid|thc|cbd|to\s+(sleep|"
    r"relax|chill)|get\s+high|buy\s+(some|a))\b",
    re.IGNORECASE,
)

# explicit "talk to a person" phrasing — feeds the human-request escalation count.
_HUMAN_REQUEST = re.compile(
    r"(talk\s+to|speak\s+(to|with)|get\s+me|connect\s+me\s+to|i\s+want)\s+(a\s+)?"
    r"(real\s+|actual\s+)?(person|human|manager|someone|representative|rep|associate|staff)",
    re.IGNORECASE,
)


def human_request_in(opener: str) -> bool:
    """Whether the opener explicitly asks for a person (the live model tracks the running count)."""
    return bool(_HUMAN_REQUEST.search(opener or ""))


def classify_intent(opener: str, *, human_requested: int = 0) -> str:
    """Classify a caller opener into one intent, applying the §4.1 precedence.

    VENDOR is checked BEFORE retail (the export-#6 fix); a dispute/defective signal or a 2nd+ human
    request OUTRANKS vendor so a hostile vendor reaches a human, not the callback loop. Ambiguous →
    ``faq`` (a safe, grounded default the FAQ member can re-route)."""
    text = opener or ""

    # 1) escalation — dispute/defective OR repeated human request (highest severity wins).
    if _DISPUTE.search(text) or human_requested >= 2:
        return INTENT_ESCALATION

    # 2) vendor — BEFORE retail (so "I've got a delivery / manifest / wholesale order" never
    #    slot-fills as a shopper).
    if _VENDOR.search(text):
        return INTENT_VENDOR

    # 3) info / FAQ.
    if _FAQ.search(text):
        return INTENT_FAQ

    # 4) retail-buyer.
    if _RETAIL.search(text):
        return INTENT_RETAIL

    # 5) ambiguous → safe FAQ default.
    return INTENT_FAQ


def classify_destination(opener: str, *, human_requested: int = 0) -> str:
    """The Squad member name the opener routes to (vendor→vendor, retail→budtender, …)."""
    return _INTENT_DESTINATION[classify_intent(opener, human_requested=human_requested)]


# ── Cartridge category from the router (P5, export weakness #4) ─────────────────
#
# The export's ONLY path to a cartridge was buried under a "concentrate" sub-branch — a caller who
# opened with "I want a cart" got slot-filled as a concentrate shopper. The fix classifies the
# CARTRIDGE category UP FRONT so the budtender member opens directly on cartridge slots (size,
# reusable-vs-disposable, effect) and never funnels through concentrate.
#
# Taxonomy parity (binding, 15-P5 §3.2): budtender's category enum is
# ``flower|concentrate|cartridge|edible|tincture`` — ``cartridge`` is a first-class top-level
# category, NEVER rewritten to ``concentrate``. The marketing_dashboard rule "Cartridge ≠
# Disposable" keeps a reusable 510 cart distinct from an all-in-one disposable, so when the caller is
# EXPLICIT ("disposable / dispo / AIO") we pass a ``subcategory`` hint; otherwise we omit it and let
# budtender's facets pick the real in-stock subtypes (never let the agent GUESS the subtype).

CATEGORY_CARTRIDGE = "cartridge"
CATEGORY_FLOWER = "flower"
CATEGORY_EDIBLE = "edible"
CATEGORY_CONCENTRATE = "concentrate"
CATEGORY_TINCTURE = "tincture"

SUBCAT_DISPOSABLE = "disposable"  # all-in-one (battery + oil)
SUBCAT_REUSABLE = "cartridge"  # a reusable 510 cart

# Cartridge trigger lexicon (15-P5 §3.2). A 510/vape-pen/AIO opener lands here UP FRONT.
_CARTRIDGE = re.compile(
    r"\b(cart|carts|cartridge|cartridges|510|vape\s*pen|vape\s*pens|vape|vapes|vaping|"
    r"disposable|disposables|dispo|dispos|all[\s-]?in[\s-]?one|aio|pod|pods)\b",
    re.IGNORECASE,
)

# The disposable / all-in-one signal — ONLY these set ``subcategory:"disposable"``. A bare
# "cart / 510 / cartridge" stays subcategory-less (reusable-vs-disposable is budtender's facet call).
_DISPOSABLE = re.compile(
    r"\b(disposable|disposables|dispo|dispos|all[\s-]?in[\s-]?one|aio)\b",
    re.IGNORECASE,
)

# Other retail-category lexicons (so a non-cartridge retail opener still classifies a category and
# never gets mis-bucketed into cartridge). Order matters: cartridge is checked FIRST below.
_EDIBLE = re.compile(
    r"\b(edible|edibles|gumm\w+|chocolate|brownie|cookie|mint|beverage|drink|seltzer|"
    r"capsule|tablet|lozenge)\b",
    re.IGNORECASE,
)
_TINCTURE = re.compile(
    r"\b(tincture|tinctures|sublingual|drops|oil\s+drops|rso|feco)\b", re.IGNORECASE
)
_CONCENTRATE = re.compile(
    r"\b(concentrate|concentrates|dab|dabs|wax|shatter|rosin|live\s+resin|badder|budder|"
    r"crumble|diamonds|sauce|distillate|hash)\b",
    re.IGNORECASE,
)
_FLOWER = re.compile(
    r"\b(flower|bud|buds|eighth|eighths|quarter|ounce|pre[\s-]?roll|pre[\s-]?rolls|joint|"
    r"smalls|shake|nug|nugs)\b",
    re.IGNORECASE,
)


def classify_category(opener: str) -> tuple[str | None, str | None]:
    """Classify a retail opener's product category + optional subcategory (P5 #4).

    Returns ``(category, subcategory)``. ``category`` is one of budtender's enum values
    (``cartridge|flower|edible|concentrate|tincture``) or ``None`` when the opener names no category
    (budtender then slot-fills it). ``subcategory`` is ``"disposable"`` ONLY when the caller was
    explicit about an all-in-one (else ``None`` — budtender's facets pick the in-stock subtype).

    Binding precedence: CARTRIDGE is checked **before** concentrate so "a 510 vape cart" never
    rewrites to ``concentrate`` (the export-#4 bug). A bare "vape/cart/510" → ``("cartridge",
    None)``; "a disposable vape pen" → ``("cartridge", "disposable")``."""
    text = opener or ""
    # 1) cartridge — FIRST, so it outranks the concentrate lexicon (the #4 fix).
    if _CARTRIDGE.search(text):
        sub = SUBCAT_DISPOSABLE if _DISPOSABLE.search(text) else None
        return CATEGORY_CARTRIDGE, sub
    # 2) edible / tincture / concentrate / flower (each a distinct top-level category).
    if _EDIBLE.search(text):
        return CATEGORY_EDIBLE, None
    if _TINCTURE.search(text):
        return CATEGORY_TINCTURE, None
    if _CONCENTRATE.search(text):
        return CATEGORY_CONCENTRATE, None
    if _FLOWER.search(text):
        return CATEGORY_FLOWER, None
    return None, None


def classify(opener: str, *, human_requested: int = 0) -> dict:
    """The §4.2 structured classifier output the ``entry_router`` mirrors: ``{intent, store?,
    category?, subcategory?}``.

    ``intent`` is the §4.1 routing decision (escalation/vendor/faq/retail). For a RETAIL intent we
    also resolve the product ``category`` (+ ``subcategory`` only when explicit) so a cartridge
    opener reaches the budtender with ``category:"cartridge"`` pre-filled — never a concentrate
    sub-branch. Non-retail intents carry no category (the specialist member owns its own slots).
    Empty/None keys are omitted so the shape matches the contract exactly."""
    intent = classify_intent(opener, human_requested=human_requested)
    out: dict = {"intent": intent}
    if intent == INTENT_RETAIL:
        category, subcategory = classify_category(opener)
        if category:
            out["category"] = category
        if subcategory:
            out["subcategory"] = subcategory
    return out
