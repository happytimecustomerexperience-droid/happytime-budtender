"""Shared text/voice agent brain.

Vapi gets transport-specific webhooks around this; website chat gets this directly.
"""

from __future__ import annotations

import re

from voice import recognition
from voice.tools import dispatch

_HUMAN_RE = re.compile(
    r"\b("
    r"human|person|manager|staff|budtender|complain|complaint|refund|"
    r"defective|broken|bad\s+cart|won'?t\s+fire|doesn'?t\s+work|unacceptable|"
    r"ripped\s+(?:me\s+)?off|rip\s*off|scam|angry|mad|upset|"
    r"wrong\s+(item|product|thing)|"
    r"incorrect\s+(order|item|product)|"
    r"missing\s+(?:\w+\s+){0,3}(?:item|product|order)|"
    r"not\s+what\s+i\s+(ordered|bought)"
    r")\b",
    re.I,
)
_CATEGORY_RE = {
    "cartridge": re.compile(r"\b(cart|carts|cartridge|vape|vapes|disposable|disposables|510|pod)\b", re.I),
    "flower": re.compile(r"\b(flower|bud|eighth|ounce|sativa|indica|hybrid)\b", re.I),
    "edible": re.compile(r"\b(edible|gummy|gummies|chocolate|drink|beverage|mg)\b", re.I),
    "concentrate": re.compile(r"\b(concentrate|dab|wax|rosin|resin|hash)\b", re.I),
    "pre-roll": re.compile(r"\b(pre.?roll|joint)\b", re.I),
}
_PRODUCT_SLOT_KEYS = (
    "category",
    "subcategory",
    "size",
    "price_tier",
    "effect_desired",
    "price_min",
    "price_max",
    "category_blocklist",
    "doh_only",
)


_CARTRIDGE_ALIAS = {
    "cart",
    "carts",
    "cartridge",
    "cartridges",
    "vape",
    "vapes",
    "disposable",
    "disposables",
    "pod",
    "pods",
}


def _normalize_category(value: str) -> str:
    cat = str(value or "").strip().lower()
    return "cartridge" if cat in _CARTRIDGE_ALIAS else cat
_SOURCE_REQUIRED_RE = re.compile(
    r"\b(returns?|refund|policy|age|wa|wac|legal|compliance|id|identification)\b",
    re.I,
)
_FAQ_FIRST_RE = re.compile(
    r"\b("
    r"specials?|deals?|discounts?|sale|hours?|open|close|location|address|phone|"
    r"returns?|refund|policy|age|wa|wac|legal|compliance|id|identification|"
    r"delivery|payment|order|defective|broken|won'?t\s+fire|doesn'?t\s+work"
    r")\b",
    re.I,
)
_PRICE_MAX_RE = re.compile(r"\b(?:under|below|less than|no more than|up to|max(?:imum)?)\s*\$?\s*(\d+(?:\.\d{1,2})?)\b", re.I)
_DOH_ONLY_RE = re.compile(r"\b(doh|medical|medically compliant|compliant)\b", re.I)
_SUBCATEGORY_RE = re.compile(r"\b(indica|sativa|hybrid)\b", re.I)
_EFFECT_ALIASES = (
    ("sleep", re.compile(r"\b(sleep|sleepy|bedtime|insomnia)\b", re.I)),
    ("relaxed", re.compile(r"\b(relax|relaxed|relaxing|calm|chill|unwind)\b", re.I)),
    ("focused", re.compile(r"\b(focus|focused|creative|energy|energized)\b", re.I)),
    ("pain relief", re.compile(r"\b(pain|ache|aches|sore|soreness)\b", re.I)),
    ("anxiety relief", re.compile(r"\b(anxiety|anxious|stress|stressed)\b", re.I)),
)
_SIZE_ALIASES = (
    ("0.5g", re.compile(r"\b(0\.5\s*g|\.5\s*g|half\s*gram)\b", re.I)),
    ("1g", re.compile(r"\b(1\s*g|one\s*gram|full\s*gram)\b", re.I)),
    ("3.5g", re.compile(r"\b(3\.5\s*g|eighth|1/8\s*oz)\b", re.I)),
    ("7g", re.compile(r"\b(7\s*g|quarter)\b", re.I)),
    ("14g", re.compile(r"\b(14\s*g|half\s*ounce|1/2\s*oz)\b", re.I)),
    ("28g", re.compile(r"\b(28\s*g|ounce|1\s*oz)\b", re.I)),
    ("5mg", re.compile(r"\b(5\s*mg)\b", re.I)),
    ("10mg", re.compile(r"\b(10\s*mg)\b", re.I)),
    ("20mg+", re.compile(r"\b(20\s*mg|25\s*mg|50\s*mg|100\s*mg)\b", re.I)),
)


def _clean_message(value) -> str:
    return " ".join(str(value or "").split())[:1000]


def _safe_store(value) -> str:
    store = str(value or "").strip().lower()
    aliases = {"mt vernon": "mount-vernon", "mt-vernon": "mount-vernon"}
    store = aliases.get(store, store)
    return store if store in {"yakima", "mount-vernon", "pullman"} else ""


def _phone_hint(data: dict) -> str:
    for value in (
        data.get("phone"),
        data.get("customer_phone"),
        (data.get("customer") or {}).get("phone") if isinstance(data.get("customer"), dict) else "",
        (data.get("session") or {}).get("phone") if isinstance(data.get("session"), dict) else "",
    ):
        digits = "".join(ch for ch in str(value or "") if ch.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            return f"+{digits}"
        if len(digits) == 10:
            return f"+1{digits}"
    return ""


def _history_text(history) -> str:
    if not isinstance(history, list):
        return ""
    lines = []
    for msg in history[-8:]:
        if not isinstance(msg, dict):
            continue
        role = "assistant" if msg.get("role") == "assistant" else "customer"
        text = _clean_message(msg.get("content"))[:500]
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _category_from_text(text: str) -> str:
    for category, pattern in _CATEGORY_RE.items():
        if pattern.search(text):
            return category
    return ""


def _price_max_from_text(text: str):
    match = _PRICE_MAX_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _subcategory_from_text(text: str) -> str:
    match = _SUBCATEGORY_RE.search(text or "")
    return match.group(1).lower() if match else ""


def _effect_from_text(text: str) -> str:
    for effect, pattern in _EFFECT_ALIASES:
        if pattern.search(text or ""):
            return effect
    return ""


def _size_from_text(text: str) -> str:
    for size, pattern in _SIZE_ALIASES:
        if pattern.search(text or ""):
            return size
    return ""


def _profile_top_category(profile_summary: dict | None) -> str:
    if not isinstance(profile_summary, dict):
        return ""

    top_categories = profile_summary.get("top_categories")
    if not isinstance(top_categories, list):
        return ""

    for item in top_categories:
        if isinstance(item, str):
            normalized = _normalize_category(item)
            if normalized in {"flower", "edible", "cartridge", "concentrate", "pre-roll"}:
                return normalized
        elif isinstance(item, dict):
            cat = item.get("category") or item.get("Category") or item.get("name") or ""
            normalized = _normalize_category(cat)
            if normalized in {"flower", "edible", "cartridge", "concentrate", "pre-roll"}:
                return normalized
        elif isinstance(item, (list, tuple)) and item:
            normalized = _normalize_category(item[0])
            if normalized in {"flower", "edible", "cartridge", "concentrate", "pre-roll"}:
                return normalized
    return ""


def _requires_sources(message: str) -> bool:
    return bool(_SOURCE_REQUIRED_RE.search(message or ""))


def _prefers_products(message: str, category: str, *, escalation: bool) -> bool:
    return bool(category) and not escalation and not _FAQ_FIRST_RE.search(message or "")


# Coarse conversation-intent label so sibling services can classify + track turns
# without re-deriving the route. Derived from the SAME signals the router acts on.
_RETURN_RE = re.compile(r"\b(returns?|refund|exchange|money\s*back|policy)\b", re.I)
_SPECIALS_RE = re.compile(r"\b(specials?|deals?|discounts?|sale|promo|coupon|bogo)\b", re.I)
_HOURS_LOC_RE = re.compile(r"\b(hours?|open|opening|close|closing|location|address|directions?|phone|parking|where\s+are)\b", re.I)


def _faq_topic(message: str) -> str:
    if _RETURN_RE.search(message or ""):
        return "return_policy"
    if _SPECIALS_RE.search(message or ""):
        return "specials"
    if _HOURS_LOC_RE.search(message or ""):
        return "hours_location"
    return ""


def _intent_label(message: str, *, escalation: bool, product: bool) -> str:
    """conflict_resolution | product_suggestion | return_policy | specials |
    hours_location | general_faq | greeting_other. Escalation wins (a dispute is a
    dispute even when grounded); an explicit product ask beats a broad FAQ match."""
    if escalation:
        return "conflict_resolution"
    if product:
        return "product_suggestion"
    topic = _faq_topic(message)
    if topic:
        return topic
    if _FAQ_FIRST_RE.search(message or ""):
        return "general_faq"
    return "greeting_other"


def _suggested_next_action(action: str) -> str:
    if action == "escalate":
        return "Please share the location and the best way for staff to follow up."
    if action == "ask_staff":
        return "I can send this to staff with your location and callback details."
    if action == "show_products":
        return "I can show those product options next."
    return ""


def _staff_followup_hint(store: str, phone: str) -> str:
    phone_hint = phone if phone else "a callback number or email"
    if store:
        return f"Please share your details for the {store} team so they can contact you at {phone_hint}."
    return f"Please share your preferred contact method ({phone_hint}) so the team can follow up."


def _normalize_suggest_picks(picks, category: str) -> list[dict]:
    if not isinstance(picks, list):
        return []
    out = []
    category_hint = f"matches your {category} request" if category else "matches your request"
    for pick in picks[:3]:
        if not isinstance(pick, dict):
            continue
        pick = dict(pick)
        pick.setdefault("why_this", f"Picked because it {category_hint}.")
        out.append(pick)
    return out


def answer_text_chat(data: dict) -> dict:
    """Answer a website chat turn through the same grounded tools Vapi uses."""

    message = _clean_message(data.get("message"))
    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    store = _safe_store(data.get("store") or data.get("location") or slots.get("store"))
    session_token = str(data.get("session_token") or data.get("session_id") or "")[:128]
    phone = _phone_hint(data)
    history = data.get("history") if isinstance(data.get("history"), list) else []
    if not message:
        return {"ok": False, "error": "message required"}

    ctx = {"store": store, "session_token": session_token, "channel": "text", "known": False}
    if phone:
        ctx["caller_number"] = phone
        ctx["_caller_phone"] = phone
        ctx.update(recognition.resolve_caller(phone, ctx) or {})
    else:
        ctx["profile_summary"] = {"has_history": False, "top_categories": [], "price_tier": ""}
    escalation = bool(_HUMAN_RE.search(message))
    category = str(slots.get("category") or _category_from_text(message)).strip()
    category = _normalize_category(category)
    if not category and not _requires_sources(message):
        category = _profile_top_category(ctx.get("profile_summary"))
    prefer_products = _prefers_products(message, category, escalation=escalation)
    faq = dispatch("faq_lookup", {"query": message, "store": store}, ctx)
    tool_results = [{"tool": "faq_lookup", "result": faq}]
    if faq.get("grounded") and not str(faq.get("answer") or "").strip():
        faq = {"grounded": False, "fallback": faq.get("fallback") or "can't confirm"}
        tool_results[0]["result"] = faq

    if _requires_sources(message) and faq.get("grounded") and not faq.get("sources"):
        faq = {"grounded": False, "fallback": "can't confirm"}
        tool_results[0]["result"] = faq

    if faq.get("grounded") and faq.get("answer") and not prefer_products:
        answer = str(faq["answer"])
        if escalation:
            answer = f"I'm sorry that happened. {answer} {_staff_followup_hint(store, phone)}"
        return {
            "ok": True,
            "intent": _intent_label(message, escalation=escalation, product=False),
            "answer": answer,
            "grounded": True,
            "sources": faq.get("sources", []),
            "tool_results": tool_results,
            "escalation_required": escalation,
            "escalation_flag": escalation,
            "safe_next_action": "escalate" if escalation else "answer",
            "safe_suggested_next_action": _suggested_next_action("escalate" if escalation else "answer"),
            "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
            "store": store,
        }

    if escalation:
        return {
            "ok": True,
            "answer": (
                "I'm sorry that happened. I can't confirm a return or refund outcome from the current "
                "Happy Time knowledge base, but I can get the store team involved. Please share the "
                "location and the best way for staff to follow up."
            ),
            "intent": "conflict_resolution",
            "grounded": False,
            "sources": [],
            "tool_results": tool_results,
            "escalation_required": True,
            "escalation_flag": True,
            "safe_next_action": "escalate",
            "safe_suggested_next_action": _suggested_next_action("escalate"),
            "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
            "store": store,
        }

    if category:
        suggest_args = {key: slots[key] for key in _PRODUCT_SLOT_KEYS if key in slots}
        if "price_max" not in suggest_args:
            price_max = _price_max_from_text(message)
            if price_max is not None:
                suggest_args["price_max"] = price_max
        if "subcategory" not in suggest_args:
            subcategory = _subcategory_from_text(message)
            if subcategory:
                suggest_args["subcategory"] = subcategory
        if "effect_desired" not in suggest_args:
            effect = _effect_from_text(message)
            if effect:
                suggest_args["effect_desired"] = effect
        if "size" not in suggest_args:
            size = _size_from_text(message)
            if size:
                suggest_args["size"] = size
        if "doh_only" not in suggest_args and _DOH_ONLY_RE.search(message):
            suggest_args["doh_only"] = True
        suggest_args["category"] = category
        if isinstance(suggest_args.get("category_blocklist"), (list, tuple)):
            suggest_args["category_blocklist"] = [
                str(item).strip().lower() for item in suggest_args["category_blocklist"] if str(item).strip()
            ]
        suggest_args.update({"category": category, "store": store})
        if isinstance(data.get("exclude_skus"), list):
            suggest_args["exclude_skus"] = data["exclude_skus"]
        suggest = dispatch(
            "suggest_products",
            suggest_args,
            ctx,
        )
        tool_results.append({"tool": "suggest_products", "result": suggest})
        picks = _normalize_suggest_picks(suggest.get("picks"), category)
        if picks:
            suggest = dict(suggest)
            suggest["picks"] = picks
            policy_context = _requires_sources(message) and not (faq.get("grounded") and faq.get("sources"))
            return {
                "ok": True,
                "intent": "product_suggestion",
                "answer": suggest.get("spoken_summary") or "I found a few in-stock options.",
                "grounded": not policy_context,
                "sources": [{"kind": "tool", "title": "Live budtender inventory"}],
                "tool_results": tool_results,
                "escalation_required": False,
                "escalation_flag": False,
                "safe_next_action": "show_products",
                "safe_suggested_next_action": _suggested_next_action("show_products"),
                "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
                "store": store,
            }
        return {
            "ok": True,
            "intent": "product_suggestion",
            "answer": "I can't find any matching items in stock right now. I can help my team check options manually if you share the best contact method.",
            "grounded": False,
            "sources": [{"kind": "tool", "title": "Live budtender inventory"}],
            "tool_results": tool_results,
            "escalation_required": False,
            "escalation_flag": False,
            "safe_next_action": "ask_staff",
            "safe_suggested_next_action": _suggested_next_action("ask_staff"),
            "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
            "store": store,
        }


    transcript = _history_text(history)
    fallback = faq.get("fallback") or "I can't confirm that from the current Happy Time knowledge base."
    answer = fallback
    if escalation:
        answer = (
            "I'm sorry that happened. I can't confirm a return or refund outcome from the current "
            "Happy Time knowledge base, but I can get the store team involved. Please share the "
            "location and the best way for staff to follow up."
        )
    elif _requires_sources(message) and not faq.get("grounded"):
        answer = f"I can't confirm that right now from the current knowledge base. {_staff_followup_hint(store, phone)}"
    return {
        "ok": True,
        "intent": _intent_label(message, escalation=escalation, product=False),
        "answer": answer,
        "grounded": False,
        "sources": [],
        "tool_results": tool_results,
        "escalation_required": escalation,
        "escalation_flag": escalation,
        "safe_next_action": "escalate" if escalation else "ask_staff",
        "safe_suggested_next_action": _suggested_next_action("escalate" if escalation else "ask_staff"),
        "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
        "store": store,
        "transcript_used": bool(transcript),
    }
