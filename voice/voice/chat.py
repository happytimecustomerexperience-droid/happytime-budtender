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
    # "money back" / "busted" are how customers actually phrase a dispute. Without them the
    # category regex wins ("busted vape pen" → cartridge) and the caller gets upsold instead.
    r"money\s*back|busted|"
    r"defective|broken|bad\s+cart|won'?t\s+fire|doesn'?t\s+work|unacceptable|"
    r"ripped\s+(?:me\s+)?off|rip\s*off|scam|angry|mad|upset|"
    r"wrong\s+(item|product|thing)|"
    r"incorrect\s+(order|item|product)|"
    r"missing\s+(?:\w+\s+){0,3}(?:item|product|order)|"
    r"not\s+what\s+i\s+(ordered|bought)"
    r")\b",
    re.I,
)
# Plural-tolerant: customers say "do you have edibles" and "what concentrates do you have".
# Only cart|carts spelled both out before, so every other plural fell through to the FAQ.
_CATEGORY_RE = {
    "cartridge": re.compile(r"\b(carts?|cartridges?|vapes?|disposables?|510|pods?)\b", re.I),
    "flower": re.compile(r"\b(flowers?|buds?|eighths?|ounces?|sativa|indica|hybrid)\b", re.I),
    "edible": re.compile(r"\b(edibles?|gummy|gummies|chocolates?|drinks?|beverages?|mg)\b", re.I),
    "concentrate": re.compile(r"\b(concentrates?|dabs?|wax|rosin|resin|hash)\b", re.I),
    "pre-roll": re.compile(r"\b(pre.?rolls?|joints?)\b", re.I),
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
# Topics the KB legitimately answers DURING a dispute. The relevance gate uses this as well as
# _FAQ_FIRST_RE: _FAQ_FIRST_RE exists to steer product-vs-FAQ preference and carries none of the
# dispute vocabulary, so gating on it alone silenced the on-topic return-policy answer a caller
# asking "do I need the receipt and the box?" should get.
_DISPUTE_TOPIC_RE = re.compile(
    # "box" is deliberately NOT here. It is too weak a dispute signal on its own ("alright, I'll
    # bring the box in" is a closing acknowledgement, not a question), and opening the gate for it
    # let retrieval answer with whatever row shared a common word — the "Do I need to bring ID?"
    # row came back for that sentence on the strength of "bring" alone. "receipt"/"packaging"
    # carry the same intent without the collision.
    r"\b(returns?|refund|money\s*back|exchange|policy|receipt|packaging|"
    r"defective|broken|busted|warranty|replacements?|replace|damaged)\b",
    re.I,
)
_PRICE_MAX_RE = re.compile(r"\b(?:under|below|less than|no more than|up to|max(?:imum)?)\s*\$?\s*(\d+(?:\.\d{1,2})?)\b", re.I)
_DOH_ONLY_RE = re.compile(r"\b(doh|medical|medically compliant|compliant)\b", re.I)
_SUBCATEGORY_RE = re.compile(r"\b(indica|sativa|hybrid)\b", re.I)
# budtender's ranker only knows these three (budtender/engine.py EFFECT_HINTS) and
# TOOL_SPECS enums to match, so a richer derived effect is DROPPED by _sanitize_args and the
# ask is ranked blind. Map down instead of losing it. (Upgrade path: teach EFFECT_HINTS
# sleep/pain/anxiety terms and widen both the enum and this map together.)
_EFFECT_TO_BUDTENDER = {
    "relaxed": "relaxed",
    "sleep": "relaxed",
    "pain relief": "relaxed",
    "anxiety relief": "relaxed",
    "focused": "uplifted",
}
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


def _recent_escalation(history) -> bool:
    """A dispute stays a dispute. Escalation was per-message regex, so a follow-up phrased
    without a trigger word ("so what are you going to do about it") silently dropped the
    handoff. Look back over the caller's own recent turns instead."""
    if not isinstance(history, list):
        return False
    for msg in history[-6:]:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        if _HUMAN_RE.search(str(msg.get("content") or "")):
            return True
    return False


def _prefers_products(message: str, category: str, *, escalation: bool) -> bool:
    return bool(category) and not escalation and not _FAQ_FIRST_RE.search(message or "")


# Safety check: MUST run before category routing. _CATEGORY_RE matches ordinary product words
# ("edibles", "gummy", "chocolate") with no idea it might be sitting inside a poisoning report, an
# impaired-driving question, or an allergen ask — so without this, _prefers_products short-circuits
# a safety turn into a sales pitch (verified: "my dog just ate one of the edibles" -> a confident
# product recommendation). These three checks win over category routing; they add a branch, they
# do not touch the existing escalation/leak-guard/Numbers-Guard paths.
_INGESTION_SUBJECT_RE = re.compile(r"\b(dog|cat|pet|child|kid|toddler|baby)\b", re.I)
_INGESTION_VERB_RE = re.compile(r"\b(ate|ingested|swallowed|got\s+into)\b", re.I)
_INGESTION_STANDALONE_RE = re.compile(
    r"\b(overdose|poison(?:ed|ing)?|throwing\s+up|won'?t\s+wake\s+up|unresponsive|emergency|"
    r"hospital|ambulance|911)\b"
    r"|too\s+much\s+and\s+(?:he|she|they)\b",
    re.I,
)
_ER_RE = re.compile(r"\bER\b")  # case-sensitive: a bare lowercase "er" is a filler word, not a signal
# "how long"/"safe" paired with "drive" would also catch "how long until I can drive after this" —
# that turn is a separate, out-of-scope retrieval-relevance bug (it answers with the wrong FAQ row,
# not a product pitch), so this only fires on the safe/ok phrasing that actually gets hijacked by
# the category regex ("is it ok to drive after one gummy").
_DRIVING_RE = re.compile(r"\b(drive|driving|behind\s+the\s+wheel)\b", re.I)
_DRIVING_QUALIFIER_RE = re.compile(r"\b(safe|ok|okay)\b", re.I)
_ALLERGEN_KEYWORD_RE = re.compile(r"\b(allerg(?:ic|y|ies)|nuts?|peanuts?|gluten|dairy|soy)\b", re.I)
_ALLERGEN_QUALIFIER_RE = re.compile(r"\b(ingredient|ingredients|contain|contains|have|has|free)\b", re.I)


def _is_ingestion_emergency(message: str) -> bool:
    return bool(
        (_INGESTION_SUBJECT_RE.search(message or "") and _INGESTION_VERB_RE.search(message or ""))
        or _INGESTION_STANDALONE_RE.search(message or "")
        or _ER_RE.search(message or "")
    )


def _is_impaired_driving_question(message: str) -> bool:
    return bool(_DRIVING_RE.search(message or "") and _DRIVING_QUALIFIER_RE.search(message or ""))


def _is_allergen_question(message: str) -> bool:
    return bool(
        _ALLERGEN_KEYWORD_RE.search(message or "") and _ALLERGEN_QUALIFIER_RE.search(message or "")
    )


def _is_safety_emergency(message: str) -> bool:
    return (
        _is_ingestion_emergency(message)
        or _is_impaired_driving_question(message)
        or _is_allergen_question(message)
    )


# Coarse conversation-intent label so sibling services can classify + track turns
# without re-deriving the route. Derived from the SAME signals the router acts on.
_RETURN_RE = re.compile(r"\b(returns?|refund|exchange|money\s*back|policy)\b", re.I)
_SPECIALS_RE = re.compile(r"\b(specials?|deals?|discounts?|sale|promo|coupon|bogo)\b", re.I)
_HOURS_LOC_RE = re.compile(
    # "located" / "closed" were missing, so "where exactly are you located" classified as nothing
    # and retrieval answered it with whatever row ranked first.
    r"\b(hours?|open|opening|close|closing|closed|location|located|address|directions?|"
    r"phone|parking|where\s+are)\b",
    re.I,
)

# A follow-up that refines the previous ask rather than starting a new one. These carry the
# caller's category forward: "keep it under 40 though" used to derive no category at all, fall
# out of the product path, and get answered with the state health warning.
_REFINEMENT_RE = re.compile(
    r"\b(cheaper|cheapest|less\s+expensive|lower|smaller|bigger|larger|stronger|weaker|"
    r"something\s+else|anything\s+else|other\s+options?|different|instead|"
    # "just the medically compliant ones" is a narrowing of the ask before it, not a new subject.
    r"medically\s+compliant|doh)\b",
    re.I,
)


def _is_refinement(message: str) -> bool:
    """A bare price word is NOT a refinement — "set those aside under the name Marcus" says
    "under" and means nothing about budget. A price ceiling only counts when it names a number,
    which is exactly what _PRICE_MAX_RE already requires."""
    if _REFINEMENT_RE.search(message or ""):
        return True
    return _price_max_from_text(message) is not None


def _carried_category(history) -> str:
    """The category from the caller's own most recent turn that named one. Only consulted for a
    refinement, so an unrelated new question never gets dragged back onto the shelf."""
    if not isinstance(history, list):
        return ""
    # Deliberately a WIDER window than _recent_escalation's. "What were we shopping for" survives
    # a tangent; a dispute should not. At 8 messages (~4 turns) a caller who asked about flower,
    # detoured through hours and specials, then said "something a bit stronger" found no category
    # in window and never reached the shelf at all — a lost sale on any call long enough to have
    # a normal conversational detour.
    for msg in reversed(history[-20:]):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        category = _normalize_category(_category_from_text(str(msg.get("content") or "")))
        if category:
            return category
    return ""


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


def _escalation_answer(store: str, phone: str) -> str:
    """The un-grounded dispute reply. Personalized through ``_staff_followup_hint`` — the previous
    hardcoded string dropped store/phone entirely, so a caller who had just read out her callback
    number was never told it had been taken down."""
    return (
        "I'm sorry that happened. I can't confirm a return or refund outcome from the current "
        "Happy Time knowledge base, but I can get the store team involved. "
        + _staff_followup_hint(store, phone)
    )


# NEW COPY — REQUIRES OWNER APPROVAL. The generic _escalation_answer ("I can't confirm a return or
# refund outcome...") is actively wrong for a pet/child ingestion report, so this case gets its own
# neutral, non-medical line: no diagnosis, no dose, no reassurance the animal/child is fine, just an
# immediate hand-off to a person or emergency services. Deliberately invents no number.
def _poison_emergency_answer(store: str, phone: str) -> str:
    return (
        "This could be an emergency. Please contact your vet, doctor, or emergency services right "
        "away — I'm not able to advise on what to do. " + _staff_followup_hint(store, phone)
    )


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
    escalation_now = bool(_HUMAN_RE.search(message))
    # A dispute carries across turns, but a clean new product ask ENDS it — otherwise a caller who
    # complains and then says "anyway, got any gummies?" never reaches the shelf. Only the
    # message's own category counts here; a profile-derived fallback must not end an escalation.
    message_category = _normalize_category(_category_from_text(message))
    carried = _recent_escalation(history) and not (message_category and not escalation_now)
    # Safety check runs before category routing and wins over it: an ingestion/poisoning report,
    # an impaired-driving question, or an allergen ask must never fall through to the ordinary
    # category regex and become a product pitch.
    is_poison_emergency = _is_ingestion_emergency(message)
    safety_hit = is_poison_emergency or _is_safety_emergency(message)
    escalation = escalation_now or carried or safety_hit
    category = str(slots.get("category") or _category_from_text(message)).strip()
    category = _normalize_category(category)
    # A refinement belongs to the ask before it. Carry the category so "keep it under 40 though"
    # re-runs the search instead of falling through to whatever the FAQ ranks first.
    # ...but only when the message is a bare refinement. A question that also reads as an FAQ
    # ("can I order ahead and pick it up?") contains refinement words incidentally and must stay
    # on the FAQ path — same guard _prefers_products uses.
    if (
        not category
        and _is_refinement(message)
        and not _requires_sources(message)
        and not _FAQ_FIRST_RE.search(message)
    ):
        category = _carried_category(history)
    if not category and not _requires_sources(message):
        category = _profile_top_category(ctx.get("profile_summary"))
    prefer_products = _prefers_products(message, category, escalation=escalation)
    # The router already classifies the subject; retrieval was never told, so "what time do you
    # close today" came back with the July specials row. Pass it so retrieval can be constrained.
    faq_args = {"query": message, "store": store}
    faq_topic = _faq_topic(message)
    if faq_topic:
        faq_args["topic"] = faq_topic
    faq = dispatch("faq_lookup", faq_args, ctx)
    # ``args`` rides along so a caller (the staff test console) can see WHICH slots the router
    # derived, not just what came back — the difference between "wrong answer" and "wrong routing".
    tool_results = [{"tool": "faq_lookup", "args": faq_args, "result": faq}]
    if faq.get("grounded") and not str(faq.get("answer") or "").strip():
        faq = {"grounded": False, "fallback": faq.get("fallback") or "can't confirm"}
        tool_results[0]["result"] = faq

    if _requires_sources(message) and faq.get("grounded") and not faq.get("sources"):
        faq = {"grounded": False, "fallback": "can't confirm"}
        tool_results[0]["result"] = faq

    # Relevance gate: retrieval always returns its best row, even when that row has nothing to do
    # with the complaint. On a dispute turn we used to wrap an apology around whatever came back
    # (an angry "wrong item" caller got read the loyalty-program row, with sources cited, so it
    # read as authoritative). Only speak a retrieved row mid-dispute when the caller actually
    # asked something the KB covers.
    speak_faq = bool(faq.get("grounded") and faq.get("answer") and not prefer_products)
    if speak_faq and escalation and not (
        _FAQ_FIRST_RE.search(message) or _DISPUTE_TOPIC_RE.search(message)
    ):
        speak_faq = False

    if speak_faq:
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
        answer = _poison_emergency_answer(store, phone) if is_poison_emergency else _escalation_answer(store, phone)
        return {
            "ok": True,
            "answer": answer,
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
                suggest_args["effect_desired"] = _EFFECT_TO_BUDTENDER.get(effect, effect)
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
        tool_results.append({"tool": "suggest_products", "args": dict(suggest_args), "result": suggest})
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
        answer = _escalation_answer(store, phone)
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
