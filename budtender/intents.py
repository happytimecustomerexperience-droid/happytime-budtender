"""Coarse conversation-intent classifier for the website chat.

The voice router (`voice.chat.answer_text_chat`) already labels each turn with an
`intent`. When we can see that label we trust it; when the voice brain is
unreachable (local dev / fallback), we classify the customer message directly so
EVERY conversation is still classified + trackable. Deterministic, no LLM call.

Taxonomy — mirrors the router's labels:
    product_suggestion | return_policy | specials | hours_location
    | conflict_resolution | general_faq | greeting_other
"""
from __future__ import annotations

import re

INTENTS = frozenset({
    "product_suggestion", "return_policy", "specials", "hours_location",
    "conflict_resolution", "general_faq", "greeting_other",
})

# Conflict/dispute (a dispute is a dispute even when a policy is quoted) — wins first.
# Includes wanting a human, which in retail chat almost always means a problem.
_CONFLICT_RE = re.compile(
    r"\b(refund|defective|broken|won'?t\s+fire|doesn'?t\s+work|scam|ripped?\s*off|"
    r"rip\s*off|unacceptable|furious|angry|complaint|complain|manager|"
    r"wrong\s+(item|product|order)|not\s+what\s+i\s+(ordered|bought)|missing\s+\w+|"
    r"(?:speak|talk)\s+to\s+(?:a\s+)?(?:real\s+)?(?:person|human|manager|rep|someone))\b", re.I)
# Strong signal: an actual product/category/strain noun. (Plurals covered explicitly
# because the surrounding \b would otherwise reject a trailing 's'.)
_PRODUCT_NOUN_RE = re.compile(
    r"\b(flower|buds?|eighths?|ounces?|indica|sativa|hybrid|cbd|thc|cartridges?|carts?|"
    r"vapes?|disposables?|edibles?|gummy|gummies|concentrates?|dabs?|wax|shatter|rosin|"
    r"resin|pre.?rolls?|joints?|strains?)\b", re.I)
# Weak signal: a shopping verb with no product noun. Ranked BELOW faq so
# "do you have loyalty points" reads as faq, not product.
_SHOP_VERB_RE = re.compile(
    r"\b(recommend|suggest|looking\s+for|show\s+me|do\s+you\s+(?:have|carry|sell|stock)|"
    r"got\s+any|in\s+stock)\b", re.I)
_RETURN_RE = re.compile(r"\b(returns?|refund|exchange|money\s*back|policy)\b", re.I)
_SPECIALS_RE = re.compile(r"\b(specials?|deals?|discounts?|sale|promos?|coupons?|bogo)\b", re.I)
_HOURS_LOC_RE = re.compile(
    r"\b(hours?|open|opening|close|closing|location|located|address|directions?|phone|parking|where)\b", re.I)
_FAQ_RE = re.compile(
    r"\b(delivery|payment|debit|credit|cash|atm|order|age|id|medical|doh|legal|compliance|"
    r"loyalty|points|rewards|preorder|pickup|curbside)\b", re.I)


def _from_message(message: str) -> str:
    m = message or ""
    if _CONFLICT_RE.search(m):
        return "conflict_resolution"
    faqish = _RETURN_RE.search(m) or _SPECIALS_RE.search(m) or _HOURS_LOC_RE.search(m)
    # A concrete product noun beats a broad FAQ match; a bare shopping verb does not.
    if _PRODUCT_NOUN_RE.search(m) and not faqish:
        return "product_suggestion"
    if _RETURN_RE.search(m):
        return "return_policy"
    if _SPECIALS_RE.search(m):
        return "specials"
    if _HOURS_LOC_RE.search(m):
        return "hours_location"
    if _FAQ_RE.search(m):
        return "general_faq"
    if _PRODUCT_NOUN_RE.search(m) or _SHOP_VERB_RE.search(m):
        return "product_suggestion"
    return "greeting_other"


def classify_intent(message: str, voice_response: dict | None = None) -> str:
    """Return the conversation intent for one turn. Trusts a valid `intent` from
    the voice router; otherwise classifies `message`."""
    if isinstance(voice_response, dict):
        label = voice_response.get("intent")
        if label in INTENTS:
            return label
    return _from_message(message)


# ── analytics roll-ups over classified conversations ──────────────────────────
def _rows(counter) -> list[dict]:
    total = sum(counter.values())
    return [{"intent": k, "n": n, "pct": round(100 * n / total) if total else 0}
            for k, n in counter.most_common()]


def conversation_breakdown(sessions) -> list[dict]:
    """Per-conversation counts by `primary_intent` (the 'marked' label), most
    common first. `sessions` is a ChatSession queryset."""
    from collections import Counter

    c = Counter(pi or "unclassified" for pi in sessions.values_list("primary_intent", flat=True))
    return _rows(c)


def intent_breakdown(events) -> list[dict]:
    """Per-turn counts by `props.intent` over user chat_message events, most
    common first. `events` is an AnalyticsEvent queryset."""
    from collections import Counter

    c = Counter()
    for props in events.values_list("props", flat=True):
        p = props or {}
        if p.get("role") == "user" and p.get("intent"):
            c[p["intent"]] += 1
    return _rows(c)
