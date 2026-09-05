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
from voice.safety_copy import CANNOT_ANSWER_SAFELY, DISPUTE, POISON_EMERGENCY, UNDER_21
from voice.tools import dispatch

_HUMAN_RE = re.compile(
    r"\b("
    r"complain|complaint|refund|"
    # "money back" / "busted" are how customers actually phrase a dispute. Without them the
    # category regex wins ("busted vape pen" → cartridge) and the caller gets upsold instead.
    r"money\s*back|busted|"
    r"defective|broken|bad\s+cart|won'?t\s+fire|doesn'?t\s+work|unacceptable|"
    r"ripped\s+(?:me\s+)?off|rip\s*off|scam|angry|mad|upset|"
    # Real anger vocabulary. "my order was wrong and I am FURIOUS" did not escalate: none of
    # angry/mad/upset appear, and the pattern below needs "wrong" BEFORE the noun.
    r"furious|livid|pissed|fed\s*up|outrageous|ridiculous|"
    r"wrong\s+(item|product|thing)|"
    # ...and the same complaint said the other way round — "my order was wrong", "the item is
    # wrong" — which is at least as common as "wrong item".
    r"(?:order|item|product|thing)\s+(?:was|is|came)\s+wrong|"
    r"incorrect\s+(order|item|product)|"
    r"missing\s+(?:\w+\s+){0,3}(?:item|product|order)|"
    r"not\s+what\s+i\s+(ordered|bought)|"
    r"refused\s+to\s+sell|turned\s+(?:me\s+)?away|misled|false\s+advertising|"
    # Register/billing disputes had NO vocabulary here at all, so "the register overcharged me
    # yesterday" carried no escalation signal and the turn fell through to unconstrained
    # retrieval, which answered a retail customer with the store's VENDOR RECEIVING StoreFact
    # (it is the row that talks about someone calling you back). A billing dispute is a dispute.
    r"over\s*charg(?:ed|e|ing)|double[\s-]?charg(?:ed|e)|charged\s+me\s+twice|"
    r"wrong\s+(?:amount|price)|billing\s+(?:error|issue|problem)|"
    r"discriminat(?:ion|ed|ing)"
    r")\b",
    re.I,
)
# BUG2 fix: "human"/"person"/"manager"/"staff"/"budtender" used to be bare words in `_HUMAN_RE`
# above, so a sentence that merely MENTIONS one of those roles ("can staff tell me in person
# then", "this is the store manager speaking") was misread as a dispute and answered with the
# conflict_resolution apology copy for a complaint that was never made. A GENUINE request for a
# human ("I want to talk to a manager", "get me a person", "can I speak to staff") must still
# escalate — that path is not weakened, only the bare-mention false-positive is removed. Each
# alternative below requires an actual request SHAPE (a verb like talk/speak/get/connect/want/
# need, or an availability question) pointed at a role noun, not just the noun appearing anywhere
# in the sentence.
_HUMAN_ROLE = r"(?:human|person|someone|manager|supervisor|staff(?:\s+member)?|budtender|rep|representative|agent)"
# 2026-09-01: the qualifier a frustrated caller actually uses — "a REAL person", "an ACTUAL human",
# "a LIVE agent" — had no slot in these alternatives, so "I want to talk to a real person" matched
# nothing at all and the turn fell through to retrieval, which grounded it on the online-ordering
# FAQ row ("...pick it up in person"). ``_HUMAN_DET`` is the optional article + optional qualifier
# every role noun below may carry; both halves are optional, so nothing that matched before stops
# matching. ``_HUMAN_ART`` is the same with the article REQUIRED — used only where dropping it
# would turn a neutral sentence into an escalation ("can someone tell me your hours").
_HUMAN_QUALIFIER = r"(?:(?:real|actual|live|human|actual\s+live)\s+)?"
_HUMAN_DET = r"(?:(?:an?|the)\s+)?" + _HUMAN_QUALIFIER
_HUMAN_ART = r"(?:an?|the)\s+" + _HUMAN_QUALIFIER
_HUMAN_REQUEST_RE = re.compile(
    r"\b(?:talk|speak|chat)\s+(?:to|with)\s+" + _HUMAN_DET + _HUMAN_ROLE + r"\b|"
    r"\b(?:get|connect|transfer)\s+me\s+(?:to\s+|with\s+)?" + _HUMAN_DET + _HUMAN_ROLE + r"\b|"
    r"\b(?:can|could|may|will)\s+(?:i|you)\s+(?:talk|speak|chat)\s+(?:to|with)\s+" + _HUMAN_DET + _HUMAN_ROLE + r"\b|"
    r"\b(?:can|could)\s+" + _HUMAN_ART + _HUMAN_ROLE + r"\b|"
    r"\bescalate\s+(?:this|it)\s+to\s+" + _HUMAN_DET + _HUMAN_ROLE + r"\b|"
    r"\bi\s+(?:want|need|would\s+like)\s+(?:to\s+(?:talk|speak|chat)\s+(?:to|with)\s+)?" + _HUMAN_DET + _HUMAN_ROLE + r"\b|"
    r"\bis\s+(?:the|a)\s+" + _HUMAN_QUALIFIER + _HUMAN_ROLE + r"\s+(?:available|there|around|in)\b|"
    r"\bis\s+there\s+a\s+" + _HUMAN_QUALIFIER + _HUMAN_ROLE + r"\b|"
    r"\blet\s+me\s+(?:talk|speak)\s+(?:to|with)\s+" + _HUMAN_DET + _HUMAN_ROLE + r"\b|"
    r"\bhave\s+(?:the|a)\s+" + _HUMAN_QUALIFIER + _HUMAN_ROLE + r"\s+call\s+me\b|"
    # "someone needs to call me back" is a request for a person said the other way round — the
    # role noun is the SUBJECT of the callback, not the object of a talk/speak verb.
    r"\b" + _HUMAN_ROLE + r"\s+(?:needs?\s+to|has\s+to|have\s+to|should|must)\s+"
    r"(?:call|phone|contact|get\s+back\s+to)\s+me\b|"
    r"\bgive\s+me\s+(?:the\s+)?(?:\w+\s+)?" + _HUMAN_ROLE + r"\b",
    re.I,
)


def _wants_human(message: str) -> bool:
    """A dispute keyword OR a genuine request to be connected to a person — never a bare mention
    of a role noun (BUG2)."""
    text = message or ""
    return bool(_HUMAN_RE.search(text) or _HUMAN_REQUEST_RE.search(text))
# Plural-tolerant: customers say "do you have edibles" and "what concentrates do you have".
# Only cart|carts spelled both out before, so every other plural fell through to the FAQ.
#
# 2026-08-10: topical/capsule/mint/infused-blunt/blunt added — these are live in-stock Dutchie
# categories that had NO pattern here at all, so a caller asking for them fell through to the
# FAQ path. "infused-blunt" is listed BEFORE "blunt": _category_from_text below returns the
# FIRST matching category (dict insertion order), and "infused blunt" contains the substring
# "blunt" — so infused-blunt must win the race or every infused-blunt ask misclassifies as blunt.
#
# 2026-08-10 FIX: flower's pattern used to also include bare "sativa|indica|hybrid" — since flower
# is checked before pre-roll/edible/concentrate/topical/etc., ANY "<strain> <category>" phrase
# ("an indica pre-roll", "sativa gummies", "hybrid concentrate") returned flower, robbing whatever
# category the caller actually named. A bare strain word now lives in ``_STRAIN_ONLY_RE`` below and
# is consulted ONLY as a fallback, after every explicit category noun here has already missed
# (two-pass, not a dict reorder — reordering just moves which category gets robbed instead of
# fixing the collision). The infused-blunt/blunt ordering above is untouched by this change.
_CATEGORY_RE = {
    "cartridge": re.compile(r"\b(carts?|cartridges?|vapes?|disposables?|510|pods?)\b", re.I),
    "flower": re.compile(r"\b(flowers?|buds?|eighths?|ounces?)\b", re.I),
    "edible": re.compile(r"\b(edibles?|gummy|gummies|chocolates?|drinks?|beverages?|mg)\b", re.I),
    "concentrate": re.compile(r"\b(concentrates?|dabs?|wax|rosin|resin|hash)\b", re.I),
    "pre-roll": re.compile(r"\b(pre.?rolls?|joints?)\b", re.I),
    "topical": re.compile(r"\b(topicals?|lotions?|balms?|creams?|salves?)\b", re.I),
    "capsule": re.compile(r"\b(capsules?|pills?|softgels?)\b", re.I),
    "mint": re.compile(r"\b(mints?)\b", re.I),
    "infused-blunt": re.compile(r"\b(infused\s*blunts?)\b", re.I),
    "blunt": re.compile(r"\b(blunts?)\b", re.I),
}
# A bare strain word with NO explicit category noun anywhere in the message implies flower —
# consulted only as the SECOND pass in ``_category_from_text``, never before the loop above.
_STRAIN_ONLY_RE = re.compile(r"\b(sativa|indica|hybrid)\b", re.I)
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
    # "fail(ed/s)" added alongside defective/broken — GAP2 fix: "the gummy that FAILED" is the
    # same broken-product family and must be recognized as still describing the disputed item
    # (see ``_ends_dispute`` below), not treated as a bare product mention.
    r"defective|broken|busted|warranty|replacements?|replace|damaged|fail(?:ed|s)?)\b",
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
# A caller naming a brand with NO category word ("anything from Wyld") is still a real product
# ask — but ``suggest_products`` has no brand slot (TOOL_SPECS in voice/constants.py; _sanitize_args
# drops any arg outside the schema) and category is REQUIRED there, so this can never become a
# normal ranked search. Narrow by design ("anything/something from <Capitalized>") so it catches
# the natural brand-ask phrasing without firing on unrelated "from <place>" mentions ("visiting
# from Idaho", "Marcus from Cascade Crest" — a vendor call, which wins earlier anyway).
_BRAND_MENTION_RE = re.compile(r"\b(?:anything|something)\s+from\s+[A-Z][\w'&-]*")
# "how much should I take for my anxiety" carries the same "anxiety" word _EFFECT_ALIASES uses for
# a shopping ask ("something for anxiety relief"), but it is a condition-dosing SAFETY question
# (test_thread_17_safety_and_compliance.py) that must never become an upsell attempt — budtenders
# legally cannot dose for a medical condition. Excluded from the effect-only product-search trigger
# below; it does not touch the existing safety-emergency branch (poison/impaired-driving/allergen).
_DOSING_QUESTION_RE = re.compile(r"\bhow\s+(?:much|many)\b[^.?!]{0,30}\btake\b", re.I)
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


def _load_escalation_state(session_token: str) -> bool:
    """GAP2 fix: the durable per-session dispute flag (``VoiceCall.escalated`` — an existing
    field, never a keyword rescan of recent history). A call's dispute status is now a property
    of the SESSION, not a ~6-message lookback window that quietly expires while a still-angry
    caller keeps talking without repeating a trigger word. Same best-effort/fail-closed discipline
    as ``_load_trusted_history``: no token, no row, or a DB error all degrade to "not escalated"
    — never guesses a caller into a dispute they never had."""
    if not session_token:
        return False
    try:
        from voice.models import VoiceCall

        call = VoiceCall.objects.filter(call_id=session_token).only("escalated").first()
        return bool(call.escalated) if call else False
    except Exception:  # noqa: BLE001 — DB unavailable degrades to "not escalated"
        return False


def _persist_trusted_turn(
    session_token: str,
    store: str,
    message: str,
    answer: str,
    latency_ms: int | None,
    *,
    escalated: bool = False,
) -> None:
    """Append this turn to the session's own durable log so the NEXT turn can trust it. Best-effort
    (a logging failure must never cost the caller their answer — same discipline as
    ``dashboard.playground._persist_turn``, which this supersedes for the VoiceCall/VoiceTurn
    writes: the console now gets its turns from here, and only adds its own tool-call trace).
    Also stamps ``VoiceCall.escalated`` (GAP2) with THIS turn's final escalation state, so the
    next turn's ``_load_escalation_state`` reads a durable flag instead of rescanning history."""
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
        if call.escalated != bool(escalated):
            call.escalated = bool(escalated)
            call.save(update_fields=["escalated"])
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
    if _STRAIN_ONLY_RE.search(text or ""):
        return "flower"
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
    """SUPERSEDED by ``_load_escalation_state`` (GAP2) as the source of truth for whether a
    dispute is still open — a durable ``VoiceCall.escalated`` flag, not a keyword rescan of the
    last few messages, which silently expired mid-dispute once the caller's own trigger words
    aged out of the window. Kept only as a defense-in-depth OR: a caller whose session row is
    unavailable (fresh DB, degraded read) still gets a within-window carry instead of losing the
    dispute outright."""
    if not isinstance(history, list):
        return False
    for msg in history[-6:]:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        if _wants_human(str(msg.get("content") or "")):
            return True
    return False


def _ends_dispute(message: str, category: str, *, escalation_now: bool) -> bool:
    """GAP2, the hard-direction half: a clean new purchase ask, or a plain hours/location pivot,
    ends a carried dispute (so "anyway, got any gummies?" reaches the shelf, and "what time do
    you close" gets an ordinary hours answer) — but a sentence that merely CONTAINS a product
    noun while still describing/referencing the problem does NOT (so "the gummy that failed" or
    "fix the moldy eighth situation" stays the dispute), and neither does a genuine dispute-topic
    question ("what's your return policy") that happens to also be an FAQ. Distinguished by
    dispute vocabulary (``_HUMAN_RE``/``_DISPUTE_TOPIC_RE``, which "fail(ed)" was added to
    alongside broken/defective) — a category or plain hours/location word with none of that
    vocabulary reads as a genuine pivot; one alongside it is still talking about the disputed
    item. Deliberately narrower than ``_FAQ_FIRST_RE`` — that regex also covers delivery/payment/
    order, and a "delivery driver" line mid-dispute must NOT end it (``test_thread_06``). A
    message that already carries its own fresh escalation trigger is handled by that trigger
    directly, not this."""
    if escalation_now or not (category or _HOURS_LOC_RE.search(message or "")):
        return False
    text = message or ""
    return not (_HUMAN_RE.search(text) or _DISPUTE_TOPIC_RE.search(text))


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


# BUG1 (dosing-advice safety gate) + scope-expansion safety categories, same precedence rule as
# the block above: these all run BEFORE category/product routing and win over it. Every check here
# is gated on ADVICE ("how much should I take", "can I mix this with my meds") never on a plain
# FACT ("how many mg are in this", "what are the purchase limits") — a fact question keeps hitting
# the ordinary FAQ/Numbers-Guard path unchanged; only a question that asks what a specific PERSON
# should personally do gets escalated here. `_DOSING_QUESTION_RE` already existed (category-
# routing's own upsell guard for "how much/many ... take"); `_DOSING_ADVICE_RE` below is additive,
# not a second overlapping pattern for the same shape.
_DOSING_ADVICE_RE = re.compile(
    r"\bhow\s+(?:much|many)\b[^.?!]{0,25}\bfor\s+(?:sleep|anxiety|pain)\b|"
    r"\bhow\s+much\b[^.?!]{0,25}\bfor\s+a\s+first[\s-]?timer\b|"
    # "how much of a gummy for my 3-year-old" — a prospective, personalized dosing-amount
    # question about a named dependent, not a shopping request (which uses add/buy/get verbs,
    # not "how much").
    r"\bhow\s+(?:much|many)\b[^.?!]{0,30}\bfor\s+my\s+(?:\d+[\s-]?(?:year|yr)s?[\s-]?old|"
    r"kid|child|toddler|baby|son|daughter)\b|"
    r"\b(?:good|typical)\s+starting\s+(?:dose|amount)\b|"
    r"\btoo\s+much\s+for\s+(?:me|someone\s+my\s+size)\b",
    re.I,
)


_LEGAL_LIMIT_RE = re.compile(
    r"\bstate\s+lines?|purchase\s+limits?|per\s+visit\b|across\s+state\b", re.I
)


def _is_dosing_advice_question(message: str) -> bool:
    """Advice ("how much should I take", "what's a good starting dose") vs. fact ("how many mg
    are in this", "what are the purchase limits") — see block comment above. A plain product-info
    numbers question is NOT caught here; it keeps hitting Numbers-Guard's honest decline. A legal
    transport/purchase-LIMIT question ("how much can I take across state lines") shares
    ``_DOSING_QUESTION_RE``'s "how much ... take" shape but is a fact/legality question, not
    personal dosing advice — excluded the same way "what are the purchase limits" already is."""
    text = message or ""
    if _LEGAL_LIMIT_RE.search(text):
        return False
    return bool(_DOSING_QUESTION_RE.search(text) or _DOSING_ADVICE_RE.search(text))


# The other half of BUG1: a caller can phrase a pure FACT/potency question ("what's the THC
# content, and don't round it") that carries none of the advice phrasing above, yet retrieval
# still grounds it on a real EducationDoc "guide" row that happens to contain concrete,
# personalized dosing instructions (a start amount, a re-dose wait time, condition-tied "use
# cases") — content a licensed WA budtender may not hand a caller as advice. `_dosing_advice_leaks`
# below is checked AFTER `faq_lookup` returns (see the call site), never on the message alone, and
# is narrow by construction: it requires BOTH an ``education``-kind source (never ``taxonomy`` —
# thread_08's "what dose should I start with" grounds on the "beginner start" taxonomy row, a
# plain defined-term fact, and must keep answering normally) AND the answer text itself actually
# containing a start/wait/re-dose instruction (never just any row that happens to mention "mg",
# e.g. the THC:CBD ratio guide's "5 mg CBD + 5 mg THC" example does not trip this).
_DOSING_ADVICE_CONTENT_RE = re.compile(
    r"\bstart(?:ing)?\s+(?:at\s+)?\d+(?:\.\d+)?\s*mg\b|"
    r"\bwait\s+\d+\s*h(?:ours?)?\b|"
    r"\bre-?dos(?:e|ing)\b",
    re.I,
)


def _dosing_advice_leaks(faq: dict) -> bool:
    if not faq.get("grounded"):
        return False
    sources = faq.get("sources") or []
    if not sources or str(sources[0].get("kind") or "") != "education":
        return False
    return bool(_DOSING_ADVICE_CONTENT_RE.search(str(faq.get("answer") or "")))


# Scope-expansion category 1/2: drug interaction. "can I take this with my blood pressure
# medication" / "I'm on Xanax" — a medication mention combined with an interaction-shaped verb.
_MEDICATION_RE = re.compile(
    r"\b(medications?|medicine|meds?|prescriptions?|xanax|adderall|ssri|antidepressants?|"
    r"blood\s+pressure|blood\s+thinners?|warfarin|chemo(?:therapy)?)\b",
    re.I,
)
_INTERACTION_VERB_RE = re.compile(
    r"\btake\s+(?:this|it|that)\s+with\b|\binteract(?:ion)?s?\s+with\b|"
    r"\bmix(?:ing)?\s+(?:this|it)\s+with\b|\bi'?m\s+on\b",
    re.I,
)


def _is_drug_interaction_question(message: str) -> bool:
    text = message or ""
    return bool(_MEDICATION_RE.search(text) and _INTERACTION_VERB_RE.search(text))


# Scope-expansion category 2/2a: a past-tense adverse event ("the gummies made my kid sick and
# I'm calling my lawyer", "gave my wife a panic attack"). Distinct from `_is_ingestion_emergency`,
# which needs an ingestion VERB (ate/swallowed/...); this covers the OUTCOME being reported
# without one. "ended up in the ER" is already covered by `_ER_RE` inside
# `_is_ingestion_emergency` — not duplicated here.
_ADVERSE_EVENT_RE = re.compile(
    r"\bmade\s+(?:\w+\s+){0,3}sick\b|"
    r"\bgot\s+(?:really\s+|very\s+)?sick\b|"
    r"\bgave\s+(?:\w+\s+){0,3}(?:a\s+)?(?:panic\s+attack|bad\s+reaction|reaction)\b|"
    r"\bhad\s+a\s+(?:bad|panic)\s+reaction\b",
    re.I,
)


def _is_adverse_event_report(message: str) -> bool:
    return bool(_ADVERSE_EVENT_RE.search(message or ""))


# Scope-expansion category 3/4: proxy/minor purchase ("can he pick it up for me", "can someone
# else pick it up for me", "can I buy this for my friend who can't come in") — must never be
# validated as a workaround, only deferred/escalated to a human.
_PROXY_PURCHASE_RE = re.compile(
    # Someone else collecting. "for me" was required before, so "pick it up for HIM" and
    # "grab it on my behalf" both walked straight past.
    r"\b(?:he|she|they|someone\s+else|my\s+\w+)\s+(?:will\s+)?(?:pick|grab)\s+(?:it\s+|them\s+|that\s+)?up\b|"
    r"\b(?:pick|grab)\s+(?:it\s+|them\s+|that\s+)?up\s+for\s+(?:me|him|her|them)\b|"
    r"\b(?:pick|grab)\s+(?:it\s+|them\s+|that\s+)?up\s+on\s+(?:my|his|her|their)\s+behalf\b|"
    r"\bon\s+my\s+behalf\b|"
    r"\bcan'?t\s+come\s+in\b|"
    r"\bbuy\s+(?:this|it|that|them)\s+for\s+my\b",
    re.I,
)

# An explicit statement of being UNDER 21. This is the licence-critical half: selling to a minor
# is a revocation offence under WAC 314-55, so "I'm under 21, can you still sell to me" must never
# get an ambiguous answer. Ages are bounded to 10-20 so that 21+ ("I'm 21", "my brother is 25")
# stays an ordinary customer, and so a bare quantity ("I want 20 pre-rolls", "20mg edibles") is
# not mistaken for an age — the age must be attached to a person.
_UNDERAGE_RE = re.compile(
    # "who's"/"who is" matters: "my friend who's 19 said he could carry it for me" is the exact
    # shape a diversion attempt takes, and it has no "my X is" or "I'm" to anchor on.
    r"\b(?:i'?m|i\s+am|he'?s|she'?s|they'?re|who'?s|who\s+is|my\s+\w+\s+is)\s+(?:1\d|20)\b|"
    r"\b(?:1\d|20)\s*(?:-|\s)?\s*(?:year|yr)s?\s*-?\s*old\b|"
    r"\bunder\s*(?:21|twenty[-\s]?one)\b|"
    r"\bunderage\b|"
    r"\bnot\s+21\s+yet\b",
    re.I,
)


def _is_proxy_purchase_question(message: str) -> bool:
    """Proxy pickup OR an explicit under-21 claim. The agent asserts no legal conclusion and
    quotes no statute — it hands the call to a person, which is the only safe answer here."""
    text = message or ""
    return bool(_PROXY_PURCHASE_RE.search(text) or _UNDERAGE_RE.search(text))


# Taking cannabis across a state line is a FEDERAL offence, and WA product may not leave WA.
# Deliberately matched on TRANSPORT intent, not on merely mentioning another state: "I'm visiting
# from Oregon, what do you recommend" is an ordinary tourist buying legally in WA and must still
# shop. It is the carrying-it-away that has to reach a person.
_INTERSTATE_RE = re.compile(
    r"\bacross\s+(?:the\s+)?state\s+lines?\b|"
    r"\bout\s+of\s+state\b|"
    r"\bback\s+home\s+with\s+me\b|"
    r"\b(?:take|bring|carry)\s+(?:it|this|them|some)\s+(?:back\s+)?(?:home|across|out\s+of\s+state)\b|"
    r"\bpassing\s+through\b|"
    r"\bfly\s+(?:home|back)\s+with\b",
    re.I,
)


def _is_interstate_transport_question(message: str) -> bool:
    return bool(_INTERSTATE_RE.search(message or ""))


# Scope-expansion follow-on: the category-routing agent's own effect-only product-search entry
# (``attempt_product_search`` below) reads "pain"/"anxiety"/"sleep" as a shopping signal even when
# the sentence is a QUESTION about the condition, not a shopping request — "what about for chronic
# pain that I've had for years" pitches an edible instead of staying off the product branch. A
# genuine shopping ask ("something for anxiety", "anything for sleep") is a REQUEST, not phrased as
# "what/how about" — narrow by construction so ordinary effect-only shopping is untouched.
_CONDITION_FOLLOWUP_RE = re.compile(r"\b(?:what|how)\s+about\b", re.I)


def _is_condition_followup_question(message: str) -> bool:
    return bool(_CONDITION_FOLLOWUP_RE.search(message or ""))


# Education guard — same precedence rule as the safety guard above: it MUST run before category
# routing. "what does indica mean" is a question ABOUT a word, not a request to be sold the thing
# the word names, but ``_STRAIN_ONLY_RE``/``_CATEGORY_RE`` see only the product noun and hand the
# turn to ``suggest_products``, which answers a definition question with a 38-dollar eighth. The
# KB has a real defined-term row for every one of these (``STRAIN_TYPE_ROWS`` /
# ``WeightTypeTaxonomy`` / the education docs), so the honest route is ``faq_lookup``.
# Narrow by construction: it needs a DEFINITION shape ("what does X mean", "what is X",
# "difference between X and Y"), never a shopping request that happens to name the same noun
# ("do you have an indica", "I want an eighth").
_EDUCATION_QUESTION_RE = re.compile(
    r"\bwhat\s+(?:does|do)\b[^.?!]{0,40}\bmean\b|"
    r"\bwhat\s+(?:is|are)\s+(?:an?\s+|the\s+)?"
    r"(?:indica|sativa|hybrid|terpenes?|thc|cbd|rso|rosin|resin|distillate|"
    r"concentrates?|edibles?|pre.?rolls?|tinctures?|eighth|quarter)\b|"
    r"\bdifference\s+between\b|"
    r"\bwhat'?s\s+the\s+difference\b|"
    r"\bwhat\s+does\s+\w+\s+stand\s+for\b",
    re.I,
)


def _is_education_question(message: str) -> bool:
    return bool(_EDUCATION_QUESTION_RE.search(message or ""))


# ── named-product stock check ───────────────────────────────────────────────────────────
# "is the Jetty Blue Dream cart in stock" is a question about ONE product, and it was answered by
# ``suggest_products`` with a DIFFERENT product (the cheapest cartridge on the shelf) — the router
# saw "cart", derived the category, and never noticed the caller had named an item. ``check_inventory``
# is the tool for this question and chat.py only ever reached it from the staging flow.
#
# LIMITATION (documented, not worked around): budtender exposes no name/query product search —
# ``budtender_client`` has ``/products/search/`` (slot-filtered) and ``/products/by-sku/`` (exact
# SKU) and nothing in between. So the name is resolved against the ranked results of the ordinary
# category search and then confirmed with ``check_inventory`` on that SKU. When the named item is
# not among them, the honest answer is that we could not confirm it — never a different product.
_STOCK_QUESTION_RE = re.compile(
    r"\b(?:is|are)\s+(?:the\s+|a\s+|any\s+)?(?P<a>[^?.!]+?)\s+(?:still\s+)?in\s+stock\b|"
    r"\bdo\s+you\s+(?:still\s+)?(?:have|carry|stock)\s+(?:the\s+|any\s+)?(?P<b>[^?.!]+?)\s+in\s+stock\b|"
    r"\b(?:do\s+you\s+)?still\s+have\s+(?:the\s+|any\s+)?(?P<c>[^?.!]+?)\s*(?:left|\?|$)",
    re.I,
)
# Words that describe HOW the caller wants to shop rather than WHICH product they mean. A residual
# name made only of these is a category browse ("do you have edibles under 20"), not a named item.
_SHOPPING_QUALIFIER_RE = re.compile(
    r"^(?:under|below|over|above|cheap|cheapest|best|good|strong|strongest|small|big|any|"
    r"some|more|other|another|left|thing|things|one|ones|\d+\S*)$",
    re.I,
)


def _named_stock_query(message: str) -> str:
    """The product NAME a stock question names, or "" when the question is really a category
    browse. A name has to survive stripping the category noun, the stopwords and the shopping
    qualifiers, and then be either two words long or an explicitly capitalized brand/strain."""
    match = _STOCK_QUESTION_RE.search(message or "")
    if not match:
        return ""
    raw = next((g for g in match.groupdict().values() if g), "").strip()
    if not raw:
        return ""
    stripped = raw
    for pattern in _CATEGORY_RE.values():
        stripped = pattern.sub(" ", stripped)
    words = [w for w in re.findall(r"[A-Za-z0-9'#-]+", stripped) if not _SHOPPING_QUALIFIER_RE.match(w)]
    words = [w for w in words if len(w) > 1 and w.lower() not in {"the", "a", "an", "you", "your", "my"}]
    if not words:
        return ""
    capitalized = [w for w in words if w[:1].isupper()]
    if len(words) >= 2 or capitalized:
        return " ".join(words)
    return ""


# ── pairing / add-on ────────────────────────────────────────────────────────────────────
# ``pair_upsell`` was registered and reachable from the phone squad, and chat.py never named it —
# so "what would go well with that" fell all the way through to the honest-miss fallback. The
# anchor is the caller's own most recently suggested SKU (``_last_suggested_sku``, the same
# durable field the staging flow already reads); with no prior suggestion there is no anchor and
# the turn keeps its existing route rather than guessing at one.
_PAIRING_RE = re.compile(
    r"\bgo(?:es)?\s+(?:well\s+)?with\s+(?:that|this|it|those|them)\b|"
    r"\bwhat\s+would\s+go\s+(?:well\s+)?with\b|"
    r"\bpair(?:s|ed|ing)?\s+(?:it\s+|that\s+|this\s+)?with\b|"
    r"\bwhat\s+else\s+should\s+i\s+(?:get|buy|grab|try|add)\b|"
    r"\banything\s+(?:else\s+)?to\s+go\s+with\b|"
    r"\bgoes?\s+good\s+with\b",
    re.I,
)


def _is_pairing_request(message: str) -> bool:
    return bool(_PAIRING_RE.search(message or ""))


def _pair_upsell_reply(sku: str, store: str, phone: str, ctx: dict, tool_results: list) -> dict:
    """One complementary add-on for the caller's own last pick, through ``pair_upsell``'s own
    strength gate. ``offer: false`` means the gate said stay quiet — so the reply suggests
    nothing rather than inventing a pairing."""
    args = {"anchor_sku": sku, "store": store}
    result = dispatch("pair_upsell", args, ctx)
    tool_results = tool_results + [{"tool": "pair_upsell", "args": dict(args), "result": result}]
    pair = result.get("pair") or {}
    if result.get("offer") and pair.get("name"):
        reason = str(result.get("reason_text") or "").strip()
        answer = f"People often add the {pair['name']}. {reason}".strip()
    else:
        # NEW COPY — REQUIRES OWNER APPROVAL.
        answer = (
            "Nothing jumps out as a natural add-on for that one. Tell me what else you're after "
            "and I'll take a look."
        )
    return {
        "ok": True,
        "intent": "product_suggestion",
        "answer": answer,
        "grounded": bool(result.get("offer")),
        "sources": [{"kind": "tool", "title": "Live budtender inventory"}],
        "tool_results": tool_results,
        "escalation_required": False,
        "escalation_flag": False,
        "safe_next_action": "show_products" if result.get("offer") else "answer",
        "safe_suggested_next_action": _suggested_next_action(
            "show_products" if result.get("offer") else "answer"
        ),
        "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
        "store": store,
    }


def _matches_name(query: str, name: str) -> bool:
    """Every distinctive word of the caller's name appears in the product's name."""
    wanted = {w.lower() for w in re.findall(r"[a-z0-9#]+", query.lower()) if len(w) > 1}
    have = {w.lower() for w in re.findall(r"[a-z0-9#]+", (name or "").lower())}
    return bool(wanted) and wanted <= have


def _stock_check_reply(
    name: str, category: str, store: str, phone: str, ctx: dict, tool_results: list
) -> dict:
    """Answer a named-product stock question with ``check_inventory`` on THAT product."""
    picks = []
    if category:
        args = {"category": category, "store": store}
        suggest = dispatch("suggest_products", args, ctx)
        tool_results = tool_results + [
            {"tool": "suggest_products", "args": dict(args), "result": suggest}
        ]
        picks = suggest.get("picks") or []
    match = next((p for p in picks if _matches_name(name, p.get("name", ""))), None)
    if match is None:
        answer = (
            "I can't confirm that specific item is in stock right now. A team member can check "
            "the shelf for you if you share the best way to reach you."
        )
        return {
            "ok": True,
            "intent": "product_suggestion",
            "answer": answer,
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

    check_args = {"sku": match.get("sku", ""), "store": store}
    check = dispatch("check_inventory", check_args, ctx)
    tool_results = tool_results + [
        {"tool": "check_inventory", "args": dict(check_args), "result": check}
    ]
    item = str(check.get("name") or match.get("name") or "that item")
    if check.get("in_stock"):
        # The only spoken value is the tool's own coarse stock band — never a figure this module
        # composed (Numbers-Guard). The price is deliberately NOT read out: the caller asked
        # whether it is on the shelf, and quoting an out-the-door figure they did not ask for is
        # how a wrong price gets spoken.
        band = str(check.get("qty_band") or "available")
        answer = f"Yes — the {item} is {band} at the moment."
    else:
        answer = f"The {item} isn't showing as in stock right now. I can help you find something similar."
    return {
        "ok": True,
        "intent": "product_suggestion",
        "answer": answer,
        "grounded": True,
        "sources": [{"kind": "tool", "title": "Live budtender inventory"}],
        "tool_results": tool_results,
        "escalation_required": False,
        "escalation_flag": False,
        "safe_next_action": "answer" if check.get("in_stock") else "show_products",
        "safe_suggested_next_action": _suggested_next_action(
            "answer" if check.get("in_stock") else "show_products"
        ),
        "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
        "store": store,
    }


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
    # "manifests" (plural) is how a driver actually asks — "when does receiving take manifests" —
    # and the singular-only \bmanifest\b missed it, so the call fell through to retail retrieval.
    r"transfer\s+manifests?|\bmanifests?\b|\bmetrc\b|\bccrs\b|\bwcia\b|"
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
# A bare "policy" used to count as a returns question, so "privacy policy" / "what do you do with my
# phone number, policy-wise" retrieved the RETURN policy on every channel (the phone agent then read
# it out). Only returns/refund/exchange vocabulary — or "return policy" itself — scopes to that topic.
_RETURN_RE = re.compile(r"\b(returns?|refund|exchange|money\s*back|return\s+policy)\b", re.I)
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


_TOPIC_VOCAB = {"return_policy": _RETURN_RE, "specials": _SPECIALS_RE, "hours_location": _HOURS_LOC_RE}


def faq_topic_fits(message: str, topic: str) -> bool:
    """Whether ``topic`` is defensible for this message — the caller's words must carry that
    topic's vocabulary. The Vapi model sometimes tags "my ID is expired, is that okay" as
    ``hours_location``; scoping retrieval to hours then hides the accepted-ID row. One rule for
    both channels: the words decide the topic, never the model's label alone."""
    rx = _TOPIC_VOCAB.get(topic)
    return bool(rx and rx.search(message or ""))


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


# The ``issue_type`` the Vapi escalation member would classify this dispute as — the same enum
# ``TOOL_SPECS["notify_staff_issue"]`` declares (voice/constants.py). Nothing is invented: a
# defect complaint is a defective_return, a bare "get me a person" is a repeated_request, and
# everything else is the generic dispute (which is also the handler's own default).
_DEFECT_RE = re.compile(
    r"\b(defective|broken|busted|damaged|won'?t\s+fire|doesn'?t\s+work|dead\s+on\s+arrival)\b", re.I
)


def _staff_issue_type(message: str) -> str:
    text = message or ""
    if _DEFECT_RE.search(text):
        return "defective_return"
    if _HUMAN_REQUEST_RE.search(text) and not _HUMAN_RE.search(text):
        return "repeated_request"
    return "dispute"


def _escalation_answer(store: str, phone: str) -> str:
    """The un-grounded dispute reply. Personalized through ``_staff_followup_hint`` — the previous
    hardcoded string dropped store/phone entirely, so a caller who had just read out her callback
    number was never told it had been taken down."""
    return DISPUTE + _staff_followup_hint(store, phone)


# NEW COPY — REQUIRES OWNER APPROVAL. The generic _escalation_answer ("I can't confirm a return or
# refund outcome...") is actively wrong for a pet/child ingestion report, so this case gets its own
# neutral, non-medical line: no diagnosis, no dose, no reassurance the animal/child is fine, just an
# immediate hand-off to a person or emergency services. Deliberately invents no number.
# NEW COPY — REQUIRES OWNER APPROVAL (GAP3). Driving/allergen safety turns escalate correctly
# but used to reuse ``_escalation_answer``'s returns/refunds wording, which is a non-sequitur for
# "is it ok to drive after one gummy" / "does the chocolate have nuts". This line is neutral: no
# medical advice, no dose, no timing, no legality ruling, no reassurance — it only says the agent
# can't answer that one and hands the caller to a person. Does not touch ``_poison_emergency_answer``
# (already owner-flagged, ingestion-specific) or ``_escalation_answer`` (genuine dispute copy).
def _cannot_answer_safely_answer(store: str, phone: str) -> str:
    return CANNOT_ANSWER_SAFELY + _staff_followup_hint(store, phone)


# NEW COPY — REQUIRES OWNER APPROVAL. A written restatement of the voice prompt's own
# ``UNDER_21_DECLINE`` (kb/seed.py ~L1085), quoted here as the source:
#
#     "IF THE CALLER IS UNDER 21: if they say they're under twenty-one, or won't confirm they're
#     twenty-one, warmly decline — let them know we can only sell to twenty-one-and-over with a
#     valid ID, that you're still happy to answer general questions, but do NOT run a product
#     search, suggest or quote a product, or take an order, and never invent a way around this."
#
# The text brain had no equivalent at all: an under-21 / proxy-purchase turn escalated (correct)
# but spoke ``_escalation_answer``'s returns-and-refunds dispute copy, a non-sequitur for a caller
# who was never disputing anything. Escalation semantics are unchanged — only the words.
def _under_21_answer(store: str, phone: str) -> str:
    return UNDER_21


def _poison_emergency_answer(store: str, phone: str) -> str:
    return POISON_EMERGENCY + _staff_followup_hint(store, phone)


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
    # BUG3 fix: a whitespace-only message (tabs/newlines, no visible characters) used to short-
    # circuit here with a bare {"ok": False, "error": ...} envelope — no answer, no intent label,
    # the one shape in the whole surface that didn't degrade safely. `_clean_message` already
    # collapses it to "", and every downstream check (`_category_from_text`, `_HUMAN_RE`, FAQ
    # retrieval) already treats "" as "nothing recognized" and lands on the ordinary honest-miss
    # fallback (same path a keyboard-mash or SQL-shaped garbage message takes) — so simply no
    # longer special-casing it here is the fix, not new fallback copy.
    message = _clean_message(data.get("message"))

    session_token = str(data.get("session_token") or data.get("session_id") or "")[:128]
    history = _load_trusted_history(session_token)
    escalation_state = _load_escalation_state(session_token)

    started = time.monotonic()
    result = _route_chat_turn(data, history, escalation_state)
    latency_ms = int((time.monotonic() - started) * 1000)

    # GAP2: persist only whether a genuine DISPUTE is still open — not this turn's full
    # ``escalation_flag`` (which is also True for a one-off safety_hit with no dispute
    # vocabulary). ``_dispute_active`` is this module's own private plumbing between turns and
    # is never part of the public answer shape a caller sees.
    dispute_active = bool(result.pop("_dispute_active", False))
    _persist_trusted_turn(
        session_token,
        str(result.get("store") or ""),
        message,
        str(result.get("answer") or ""),
        latency_ms,
        escalated=dispute_active,
    )
    return result


def _route_chat_turn(data: dict, history: list[dict], escalation_state: bool = False) -> dict:
    """All the actual routing logic. ``history`` arrives server-reconstructed (see
    ``answer_text_chat``/module trust-boundary comment) — this function never reads
    ``data["history"]`` itself. ``escalation_state`` is this session's durable
    ``VoiceCall.escalated`` flag (GAP2), reconstructed the same trusted way."""

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
    escalation_now = _wants_human(message)
    # A dispute carries across turns, but a clean new product ask ENDS it — otherwise a caller who
    # complains and then says "anyway, got any gummies?" never reaches the shelf. Only the
    # message's own category counts here; a profile-derived fallback must not end an escalation.
    message_category = _normalize_category(_category_from_text(message))
    # GAP2: the durable per-session flag is the source of truth; the window rescan is kept only
    # as an OR fallback for a session whose row read degraded (see ``_recent_escalation``'s
    # updated docstring). Ending the carry is no longer "any category word" — see
    # ``_ends_dispute`` for the both-directions boundary (clean new ask vs. still-the-dispute).
    still_open = escalation_state or _recent_escalation(history)
    carried = still_open and not _ends_dispute(message, message_category, escalation_now=escalation_now)
    # Safety check runs before category routing and wins over it: an ingestion/poisoning report,
    # an impaired-driving question, or an allergen ask must never fall through to the ordinary
    # category regex and become a product pitch.
    is_poison_emergency = _is_ingestion_emergency(message)
    safety_hit = (
        is_poison_emergency
        or _is_safety_emergency(message)
        or _is_dosing_advice_question(message)
        or _is_drug_interaction_question(message)
        or _is_adverse_event_report(message)
        or _is_proxy_purchase_question(message)
        # NOTE: interstate transport is deliberately NOT a safety escalation. Tried it; it made
        # things worse. The KB has a real, correct, citable row ("product must stay in
        # Washington"), so escalating turns an accurate cited answer into a handoff — worse
        # service, not safer. The genuine defect there is a RETRIEVAL one (the question sometimes
        # lands on the return-policy row instead, because its WAC citation shares the phrase
        # "Washington state law"), and the fix for that is topic-constrained retrieval, not a
        # blanket escalation. Left as a documented gap rather than papered over with a handoff.
    )
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
    # An effect ("help me relax") or a named brand ("anything from Wyld") is a real product ask
    # even with no category word and nothing carried/profiled. suggest_products can't be given a
    # category-less search on purpose (it has no brand slot and category is REQUIRED — see
    # ``_BRAND_MENTION_RE``'s comment) — but letting the FAQ's semantic search speak whatever
    # unrelated row it ranks first (verified: a walk-in/ID-policy row for "help me relax") is worse
    # than actually trying the shelf: ``handle_suggest_products`` own missing-category guard
    # already gives an honest, non-invented miss instead. Same FAQ/requires_sources guard the
    # refinement-carry logic above uses, so a message that ALSO reads as a genuine FAQ ask stays on
    # the FAQ path.
    attempt_product_search = bool(
        not category
        and not escalation
        and (_effect_from_text(message) or _BRAND_MENTION_RE.search(message or ""))
        and not _requires_sources(message)
        and not _FAQ_FIRST_RE.search(message)
        and not _DOSING_QUESTION_RE.search(message or "")
        and not _is_condition_followup_question(message)
    )
    # Education guard (see ``_EDUCATION_QUESTION_RE``): a definition question keeps its product
    # noun but loses the product route, so it reaches the KB's defined-term row instead of the
    # shelf. Placed with the other pre-routing guards, after category derivation so nothing else
    # has to know about it.
    if _is_education_question(message):
        category = ""
        attempt_product_search = False
    prefer_products = _prefers_products(message, category, escalation=escalation) or attempt_product_search
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

    # BUG1 (second half — see `_dosing_advice_leaks` docstring): the message itself may read as an
    # ordinary fact/potency question, but the row retrieval actually grounded on carries concrete,
    # personalized dosing instructions. Escalate instead of speaking it. Checked here (post-
    # dispatch, pre-speak-decision) so it wins over the vendor/staging gates and the grounded-FAQ
    # speak decision below, same precedence as every other safety category. Gated on
    # ``not prefer_products``: when the product branch is what's actually going to answer this
    # turn (e.g. "add a couple of those raspberry gummies for my wife too" — ordinary shopping
    # that merely retrieves the Edibles guide as faq_lookup's incidental top hit), that grounded
    # row is never spoken to the caller either way, so there is nothing to leak and no reason to
    # divert an ordinary sale into an escalation.
    if not escalation and not prefer_products and _dosing_advice_leaks(faq):
        escalation = True
        safety_hit = True

    # Vendor/staging gates (ADDED precedence — see the block comment above their definitions):
    # both lose to escalation/safety, and both win over the grounded-FAQ speak decision and the
    # product branch below, so a vendor pitch never slot-fills as retail and a staging request
    # never gets answered with irrelevant online-order hold copy.
    if not escalation and _is_vendor_call(message):
        return _vendor_callback_reply(message, store, phone, ctx, tool_results)

    if not escalation and _is_staging_request(message):
        return _stage_cart_reply(ctx, store, phone, tool_results)

    # The text channel had no equivalent of the Vapi escalation member's ``notify_staff_issue``
    # call at all (grep: the tool was registered and reachable from the phone squad, and this
    # module never named it) — a genuine dispute raised the escalation flag and asked for a
    # callback number, but NOTHING ever reached the store team. When the caller's number is
    # already on the session there is nothing left to gather, so file the alert here with the
    # same args the escalation member sends. Gated on a real DISPUTE (a fresh or carried
    # ``_wants_human`` trigger), never on a bare ``safety_hit`` — a dosing/allergen question is
    # not a staff complaint. The tool is idempotent per ``ctx['call_id']``, so a multi-turn
    # dispute updates the one durable record instead of spamming the team.
    if (escalation_now or carried) and phone:
        staff_args = {
            "store": store,
            "issue_type": _staff_issue_type(message),
            "summary": message,
        }
        staff_result = dispatch("notify_staff_issue", staff_args, ctx)
        tool_results.append(
            {"tool": "notify_staff_issue", "args": dict(staff_args), "result": staff_result}
        )

    # Relevance gate: retrieval always returns its best row, even when that row has nothing to do
    # with the complaint. On a dispute turn we used to wrap an apology around whatever came back
    # (an angry "wrong item" caller got read the loyalty-program row, with sources cited, so it
    # read as authoritative). Only speak a retrieved row mid-dispute when the caller actually
    # asked something the KB covers.
    speak_faq = bool(faq.get("grounded") and faq.get("answer") and not prefer_products)
    if speak_faq and escalation and not (
        _FAQ_FIRST_RE.search(message)
        or _DISPUTE_TOPIC_RE.search(message)
        # GAP1 fix: the gate's vocabulary omitted purchase-limit/interstate compliance topics
        # the KB genuinely answers, so a real mid-escalation question about them ("what are the
        # purchase limits", "how much can I take across state lines") silently lost its cited
        # answer and deferred to a human instead. Reuses ``_LEGAL_LIMIT_RE`` (already the exact
        # vocabulary for those two topics, defined above) rather than widening ``_FAQ_FIRST_RE``
        # itself — widening that shared regex would also touch product-routing/intent-labeling
        # call sites this fix has no business touching. ID/age/hours/payment are already covered
        # by ``_FAQ_FIRST_RE`` above; deliberately NOT widened with loyalty/specials vocabulary —
        # that is exactly the original bug (an angry "wrong item" caller read the loyalty-program
        # row), pinned by ``test_thread_02``/``test_thread_03``.
        or _LEGAL_LIMIT_RE.search(message)
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
            # GAP2, private plumbing: only a genuine DISPUTE (a fresh ``_wants_human`` trigger, or
            # one carried from a prior turn) is durably remembered for the NEXT turn. A bare
            # safety_hit (dosing-advice, driving, allergen, interaction, ...) with no dispute
            # vocabulary must NOT bleed forward and silently escalate an unrelated later question
            # (regression pinned by ``test_thread_17``) — that stays the existing per-message
            # safety check, untouched. Stripped before the result reaches any caller.
            "_dispute_active": escalation_now or carried,
            "safe_next_action": "escalate" if escalation else "answer",
            "safe_suggested_next_action": _suggested_next_action("escalate" if escalation else "answer"),
            "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
            "store": store,
        }

    if escalation:
        # GAP3: a safety escalation gets neutral non-medical copy instead of the returns/refunds
        # dispute apology. Poison-emergency keeps its own dedicated line; a genuine DISPUTE keeps
        # the dispute copy.
        #
        # 2026-09-01: condition-dosing ("how much should I take for my anxiety") and drug
        # interaction ("can I take this with my blood pressure medication") were escalating with
        # ``_escalation_answer``'s "I can't confirm a return or refund outcome" — a non-sequitur
        # for a caller who is not disputing anything. They are the SAME class of question as
        # driving/allergen (the agent cannot answer it safely), so they now pick the same
        # already-owner-signed copy. No new wording.
        is_cannot_answer_safely = not is_poison_emergency and (
            _is_impaired_driving_question(message)
            or _is_allergen_question(message)
            or _is_dosing_advice_question(message)
            or _is_drug_interaction_question(message)
        )
        if is_poison_emergency:
            answer = _poison_emergency_answer(store, phone)
        elif is_cannot_answer_safely:
            answer = _cannot_answer_safely_answer(store, phone)
        elif _is_proxy_purchase_question(message):
            answer = _under_21_answer(store, phone)
        else:
            answer = _escalation_answer(store, phone)
        return {
            "ok": True,
            "answer": answer,
            "intent": "conflict_resolution",
            "grounded": False,
            "sources": [],
            "tool_results": tool_results,
            "escalation_required": True,
            "escalation_flag": True,
            "_dispute_active": escalation_now or carried,  # GAP2 plumbing — see note above
            "safe_next_action": "escalate",
            "safe_suggested_next_action": _suggested_next_action("escalate"),
            "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
            "store": store,
        }

    # A stock question about a NAMED product is answered about that product (see
    # ``_named_stock_query``), not with whatever the ranker likes best in its category.
    stock_name = _named_stock_query(message)
    if stock_name:
        return _stock_check_reply(stock_name, category, store, phone, ctx, tool_results)

    # "what would go well with that" is an add-on question about the caller's OWN last pick (see
    # ``_PAIRING_RE``). With no prior suggestion there is no anchor to pair against, so the turn
    # keeps whatever route it had rather than guessing at one.
    if _is_pairing_request(message):
        anchor_sku = _last_suggested_sku(ctx.get("call_id") or session_token)
        if anchor_sku:
            return _pair_upsell_reply(anchor_sku, store, phone, ctx, tool_results)

    # A specials turn is answered by ``faq_lookup``'s specials branch either way — with the deals
    # that are actually running, or with the honest "nothing posted right now". Falling through
    # to the shelf when no deal is current would quietly swap the caller's question ("any
    # specials on edibles?") for a product pitch that never mentions a deal at all.
    if faq_topic == "specials":
        category = ""
        attempt_product_search = False

    if category or attempt_product_search:
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
        "_dispute_active": escalation_now or carried,  # GAP2 plumbing — see note above
        "safe_next_action": "escalate" if escalation else "ask_staff",
        "safe_suggested_next_action": _suggested_next_action("escalate" if escalation else "ask_staff"),
        "contact_hint": {"store": store, "customer_phone": phone} if phone or store else None,
        "store": store,
        "transcript_used": bool(transcript),
    }
