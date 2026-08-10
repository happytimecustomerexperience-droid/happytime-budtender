"""Shared text/voice agent brain.

Vapi gets transport-specific webhooks around this; website chat gets this directly.

── TRUST BOUNDARY (2026-08-10) ──────────────────────────────────────────────────────────
``answer_text_chat`` is reachable directly over HTTP (the Bearer-gated ``/api/voice/chat``,
``voice/api.py``) and from the staff playground console (``dashboard/playground.py``). Both
callers are authenticated as "a legitimate client of the service" — NEITHER authenticates
which end-caller/session a given request belongs to. That distinction matters because a
conversation turn's ``escalation``/product-``category`` state used to be derived straight
from a client-supplied ``history`` array (``data["history"]``), with zero check that the
array actually belonged to the phone/session on the SAME request. A caller (or a buggy/
malicious client) could hand this endpoint any ``history`` it liked and inherit another
caller's dispute or shopping context (pinned by ``voice/tests/test_caller_identity_bleed.py``).

TRUSTED — this module's own durable record of a session's turns:
    ``VoiceCall``/``VoiceTurn`` rows (``voice/models.py``), keyed on ``call_id ==
    session_token``. Every turn this module answers, it also WRITES to those rows
    (``_persist_trusted_turn``) before returning; every turn it answers, it reconstructs
    that session's history by READING those same rows (``_load_trusted_history``) — never
    from the request body. A session_token's history is therefore exactly what THIS module
    itself said and heard for that session_token; nothing else can inject into it.

NOT TRUSTED — the client-supplied ``history`` field. It is no longer read anywhere in this
    module. (It is still accepted on the wire for backward compatibility with older
    clients/tests that send it — it is simply ignored.)

WHY ``session_token`` IS AN ADEQUATE KEY: it is exactly what callers already resend turn
    over turn to mean "same conversation" (the playground console does this today — see
    ``dashboard/playground.py``/``templates/dashboard/playground.html``). A brand-new or
    absent ``session_token`` has, by construction, no prior rows — a fresh caller starts
    with empty history, never someone else's. Reconstruction is best-effort: if the DB is
    unreachable the fallback is NO history (fail closed — never fall back to trusting the
    client's array). A caller who somehow learns another session's token could still read
    that session's history back (the residual risk of any session-token scheme); tokens here
    are server-generated UUIDs (``dashboard/playground._new_call_id``) or budtender-issued,
    not guessable, and this module does not change that model — it only stops trusting an
    ARBITRARY client-supplied array with no token check at all.

OUT OF SCOPE — Vapi/the phone agent: it never calls ``answer_text_chat``. Vapi drives tools
    directly through the signed webhook (``voice/webhooks.py``), which has its own
    call_id-keyed turn log and is untouched by this change.
──────────────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
import time

from voice import recognition, vendor_flow
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
#
# 2026-08-10: topical/capsule/mint/infused-blunt/blunt added — these are live in-stock Dutchie
# categories that had NO pattern here at all, so a caller asking for them fell through to the
# FAQ path. "infused-blunt" is listed BEFORE "blunt": _category_from_text below returns the
# FIRST matching category (dict insertion order), and "infused blunt" contains the substring
# "blunt" — so infused-blunt must win the race or every infused-blunt ask misclassifies as blunt.
_CATEGORY_RE = {
    "cartridge": re.compile(r"\b(carts?|cartridges?|vapes?|disposables?|510|pods?)\b", re.I),
    "flower": re.compile(r"\b(flowers?|buds?|eighths?|ounces?|sativa|indica|hybrid)\b", re.I),
    "edible": re.compile(r"\b(edibles?|gummy|gummies|chocolates?|drinks?|beverages?|mg)\b", re.I),
    "concentrate": re.compile(r"\b(concentrates?|dabs?|wax|rosin|resin|hash)\b", re.I),
    "pre-roll": re.compile(r"\b(pre.?rolls?|joints?)\b", re.I),
    "topical": re.compile(r"\b(topicals?|lotions?|balms?|creams?|salves?)\b", re.I),
    "capsule": re.compile(r"\b(capsules?|pills?|softgels?)\b", re.I),
    "mint": re.compile(r"\b(mints?)\b", re.I),
    "infused-blunt": re.compile(r"\b(infused\s*blunts?)\b", re.I),
    "blunt": re.compile(r"\b(blunts?)\b", re.I),
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


def _load_trusted_history(session_token: str) -> list[dict]:
    """This session's OWN prior turns, reconstructed from ``VoiceCall``/``VoiceTurn`` — see the
    module-level trust-boundary comment. Never reads ``data["history"]``. Best-effort: no
    session_token, no row for it yet, or a DB error all degrade to "no history" (empty),
    never to trusting anything the client sent."""
    if not session_token:
        return []
    try:
        from voice.models import VoiceCall

        call = VoiceCall.objects.filter(call_id=session_token).first()
        if not call:
            return []
        return [{"role": turn.role, "content": turn.text} for turn in call.turns.order_by("seq")]
    except Exception:  # noqa: BLE001 — DB unavailable degrades to no history, never to the client array
        return []


def _persist_trusted_turn(session_token: str, store: str, message: str, answer: str, latency_ms: int | None) -> None:
    """Append this turn to the session's own durable log so the NEXT turn can trust it. Best-effort
    (a logging failure must never cost the caller their answer — same discipline as
    ``dashboard.playground._persist_turn``, which this supersedes for the VoiceCall/VoiceTurn
    writes: the console now gets its turns from here, and only adds its own tool-call trace)."""
    if not session_token:
        return
    try:
        from voice import guardrails
        from voice.models import VoiceCall, VoiceTurn

        call, _ = VoiceCall.objects.get_or_create(call_id=session_token, defaults={"store": store})
        seq = call.turns.count()
        VoiceTurn.objects.create(call=call, seq=seq, role="user", text=guardrails.redact_pii(message)[:4000])
        VoiceTurn.objects.create(
            call=call,
            seq=seq + 1,
            role="assistant",
            text=guardrails.redact_pii(answer)[:4000],
            latency_ms=latency_ms,
        )
    except Exception:  # noqa: BLE001 — best-effort; the answer already went out
        pass


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


# ── vendor / phone-cart gates (2026-08-10 GAP fix) ──────────────────────────────────────
# Two tools were registered (``notify_vendor_callback``, ``stage_phone_cart``) and reachable from
# the phone squad, but had NO branch in this shared brain at all — a web-chat vendor was shopped
# and a web-chat staging request was answered with irrelevant online-order copy. These two gates
# are ADDED precedence, inserted after escalation/safety and before the grounded-FAQ speak
# decision + the product branch (see the call site) — they do not reorder anything that already
# existed. Both LOSE to escalation (the caller checks it before calling either _is_* function),
# so a hostile "delivery" mention from an angry customer still escalates instead of logging a
# callback (the caller-facing requirement this whole gate is built around).
#
# The vendor lexicon is DELIBERATELY narrower than ``voice/routing.py``'s ``_VENDOR`` regex (that
# module classifies a phone call's cold OPEN, once, before any retail signal exists). Mid-chat,
# a retail shopper legitimately says "do you offer delivery" (test_intent_label.py) or a
# margin-fisher says "...wholesale, I mean" while pointing at a product already on screen
# (test_thread_11) — bare "delivery"/"wholesale" would misroute both. Every alternative below
# requires the caller to be identifying themselves or their business, not just naming a word a
# retail customer could also say.
_VENDOR_RE = re.compile(
    r"\b("
    r"sales\s*rep(?:resentative)?s?|"
    r"i'?m\s+a\s+vendor|vendor\s+account|vendor\s+callback|"
    r"wholesale\s+(pricing|order|orders|account|accounts|purchasing|distributor|rep|reps|"
    r"representative)|calling\s+about\s+wholesale|"
    r"distributor|"
    r"purchasing\s+manager|handles?\s+purchasing|purchasing\s+(department|team)|"
    r"(?:is\s+)?your\s+buyer\s+(?:available|there|in)|the\s+buyer\s+available|"
    r"delivery\s+driver|i'?m\s+the\s+driver|dropping\s+off\s+a\s+delivery|here\s+with\s+a\s+delivery|"
    r"transfer\s+manifest|\bmanifest\b|\bmetrc\b|\bccrs\b|\bwcia\b|"
    r"purchase\s+order|"
    r"\binvoice\b|accounts?\s+payable|"
    r"sample\s+drop"
    r")\b",
    re.I,
)

# Staging/hold-for-pickup lexicon. Requires a real hold-request shape ("set X aside", "hold X for
# me/until", "put me down for", "pick it up later") — NOT a bare "hold on" filler (thread 08/14) or
# a status QUESTION about whether something is already held ("is anything being held", thread 07 —
# note the past tense "held", never matched by the literal "hold" below).
_STAGE_RE = re.compile(
    r"\b("
    r"set\s+(?:\w+\s+){0,3}aside|"
    r"hold\s+(?:\w+\s+){0,4}(?:for\s+me|until)|"
    r"put\s+me\s+down\s+for|"
    r"pick\w*\s+(?:it\s+)?up\s+later"
    r")\b",
    re.I,
)


def _is_vendor_call(message: str) -> bool:
    return bool(_VENDOR_RE.search(message or ""))


def _is_staging_request(message: str) -> bool:
    return bool(_STAGE_RE.search(message or ""))


def _last_suggested_sku(call_id: str) -> str:
    """The most recently suggested SKU for this session — read from ``VoiceCall.suggested_skus``,
    the SAME durable field ``suggest.py``'s ``_stamp_suggested`` already appends to (keyed on
    ``ctx['call_id']``, which ``_route_chat_turn`` sets to the session_token for text chat — a web
    visitor has no Vapi call.id). Web chat has no "current product" concept of its own, so the last
    SKU actually suggested is the only non-invented signal available for a staging request that
    names no SKU. Best-effort; a DB error or no prior suggestion both degrade to "" (never guesses,
    never fabricates a SKU)."""
    call_id = str(call_id or "").strip()
    if not call_id:
        return ""
    try:
        from voice.models import VoiceCall

        call = VoiceCall.objects.filter(call_id=call_id).only("suggested_skus").first()
    except Exception:  # noqa: BLE001 — sku resolution must never crash the turn
        return ""
    skus = list(call.suggested_skus or []) if call else []
    return str(skus[-1]) if skus else ""


def _vendor_callback_reply(message: str, store: str, phone: str, ctx: dict, tool_results: list) -> dict:
    """Route a detected vendor/sales-rep/delivery-driver caller to ``notify_vendor_callback``
    instead of the retail/FAQ paths. Idempotent per ``ctx['call_id']`` (the tool's own contract —
    a re-delivered/repeated vendor-sounding turn within the same session confirms, never
    duplicates the durable record or re-fires the staff alert)."""
    args = {
        "store": store,
        "reason": vendor_flow.normalize_reason(message),
        "summary": message,
    }
    result = dispatch("notify_vendor_callback", args, ctx)
    tool_results = tool_results + [{"tool": "notify_vendor_callback", "args": dict(args), "result": result}]
    answer = str(result.get("spoken") or "").strip() or (
        "Got it — I've let the team know and someone will follow up with you soon."
    )
    return {
        "ok": True,
        "intent": "vendor_callback",
        "answer": answer,
        "grounded": False,
        "sources": [],
        "tool_results": tool_results,
        "escalation_required": False,
        "escalation_flag": False,
        "safe_next_action": "answer",
        "safe_suggested_next_action": _suggested_next_action("answer"),
        "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
        "store": store,
    }


def _stage_cart_reply(ctx: dict, store: str, phone: str, tool_results: list) -> dict:
    """Route a detected staging/hold request to ``stage_phone_cart``. Conservative by design: the
    SKU comes ONLY from ``_last_suggested_sku`` (the caller's own most recently suggested pick,
    never invented); when that resolves to nothing, the honest answer is to say so and offer a
    human — never stage a guessed item. ``stage_phone_cart`` takes no phone argument by design
    (``voice/tools/phone_cart.py`` injects it server-side from ``ctx['_caller_phone']``/
    ``ctx['caller_number']``) — that contract is untouched here."""
    sku = _last_suggested_sku(ctx.get("call_id") or ctx.get("session_token") or "")
    if not sku:
        answer = (
            "I don't have a specific item pulled up yet to set aside — let's find one first, or I "
            "can have my team hold something for you if you tell me what you're looking for."
        )
        return {
            "ok": True,
            "intent": "phone_cart_staged",
            "answer": answer,
            "grounded": False,
            "sources": [],
            "tool_results": tool_results,
            "escalation_required": False,
            "escalation_flag": False,
            "safe_next_action": "ask_staff",
            "safe_suggested_next_action": _suggested_next_action("ask_staff"),
            "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
            "store": store,
        }

    args = {"action": "add_item", "store": store, "sku": sku, "quantity": 1}
    result = dispatch("stage_phone_cart", args, ctx)
    tool_results = tool_results + [{"tool": "stage_phone_cart", "args": dict(args), "result": result}]
    ok = bool(result.get("ok"))
    # Name the exact item that was staged (a check_inventory lookup, not an invented name) so the
    # caller can correct it on the spot if it is not what they meant — only ONE sku is ever staged
    # per turn (the most recent pick), so silence about WHICH one would risk the caller assuming
    # everything they mentioned was held.
    if ok:
        check = dispatch("check_inventory", {"sku": sku, "store": store}, ctx)
        name = str(check.get("name") or "").strip()
        item = f"the {name}" if name else "that item"
        summary = str(result.get("spoken_summary") or "").strip()
        answer = f"I've set {item} aside for pickup. {summary}".strip()
    else:
        answer = str(result.get("spoken_summary") or "").strip() or (
            "I could not stage that cart change right now. A team member can help finish it."
        )
    return {
        "ok": True,
        "intent": "phone_cart_staged",
        "answer": answer,
        "grounded": ok,
        "sources": [],
        "tool_results": tool_results,
        "escalation_required": False,
        "escalation_flag": False,
        "safe_next_action": "answer" if ok else "ask_staff",
        "safe_suggested_next_action": _suggested_next_action("answer" if ok else "ask_staff"),
        "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
        "store": store,
    }


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
    """Answer a website chat turn through the same grounded tools Vapi uses.

    Entry point + trust boundary (see module docstring): binds this turn to the caller's OWN
    session-side history — reconstructed from this module's own durable log, never from the
    request body's ``history`` — before handing off to the routing logic, then appends the
    turn to that log so the NEXT turn on this session_token can trust it in turn.
    """
    message = _clean_message(data.get("message"))
    if not message:
        return {"ok": False, "error": "message required"}

    session_token = str(data.get("session_token") or data.get("session_id") or "")[:128]
    history = _load_trusted_history(session_token)

    started = time.monotonic()
    result = _route_chat_turn(data, history)
    latency_ms = int((time.monotonic() - started) * 1000)

    _persist_trusted_turn(session_token, str(result.get("store") or ""), message, str(result.get("answer") or ""), latency_ms)
    return result


def _route_chat_turn(data: dict, history: list[dict]) -> dict:
    """All the actual routing logic. ``history`` arrives server-reconstructed (see
    ``answer_text_chat``/module trust-boundary comment) — this function never reads
    ``data["history"]`` itself."""

    message = _clean_message(data.get("message"))
    slots = data.get("slots") if isinstance(data.get("slots"), dict) else {}
    store = _safe_store(data.get("store") or data.get("location") or slots.get("store"))
    session_token = str(data.get("session_token") or data.get("session_id") or "")[:128]
    phone = _phone_hint(data)

    ctx = {"store": store, "session_token": session_token, "channel": "text", "known": False}
    # Text chat has no Vapi call.id — reuse session_token consistently (the same key
    # ``_persist_trusted_turn``/``_load_trusted_history`` already use) so ``suggest.py``'s
    # ``_stamp_suggested`` and ``notify_vendor_callback`` both persist onto the SAME durable
    # ``VoiceCall`` row this session's history is already tracked on.
    if session_token:
        ctx["call_id"] = session_token
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

    # Vendor/staging gates (ADDED precedence — see the block comment above their definitions):
    # both lose to escalation/safety, and both win over the grounded-FAQ speak decision and the
    # product branch below, so a vendor pitch never slot-fills as retail and a staging request
    # never gets answered with irrelevant online-order hold copy.
    if not escalation and _is_vendor_call(message):
        return _vendor_callback_reply(message, store, phone, ctx, tool_results)

    if not escalation and _is_staging_request(message):
        return _stage_cart_reply(ctx, store, phone, tool_results)

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
