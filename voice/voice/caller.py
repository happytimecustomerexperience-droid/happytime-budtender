"""Who is calling: the per-call caller context, and the words the phone agents say with it.

One budtender lookup per call (``POST /customer/caller-context``) is cached under ``caller:<call_id>``
for two hours, so every later tool-call POST and every squad member reads the same answer. What is
cached is a first name and a taste summary. It is never the phone number, and nothing is written to
this repo's own database (see ``crm/CLAUDE.md``). ``{}`` means UNKNOWN (budtender unreachable, no
number, recognition switched off); a resolved context with no name means a person we have no name
for. They are different: unknown is never cached and never treated as "a new caller".

Code owns the words. The greeting and the CALLER line are built here from validated fields, and every
string inserted into the prompt is sanitised, so vendor-controlled data (a brand name) cannot carry an
instruction. The model only ever receives the finished text.

The cache is Django's default cache: shared across workers only when ``HHT_CACHE_URL`` is set or
``HHT_USE_CELERY=1`` (config/settings.py). On a per-process cache a second worker just looks the
caller up again (the endpoint is idempotent), so correctness holds, one extra budtender call per
worker.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core.cache import cache

from voice import capabilities

logger = logging.getLogger(__name__)

TTL_SECONDS = 2 * 60 * 60
FETCH_TIMEOUT = 2.5  # seconds, connect + read together: Vapi's assistant-request answer is due in 7.5

# The members that get remember_caller (the greeter and the retail agent). kb/seed.py keeps its own
# copy for the prompt rule; test_caller_greeting pins that the two agree.
NAME_ROLES = ("entry_router", "budtender")
# Tools whose behaviour depends on who is calling: the webhook resolves the caller before these run.
IDENTITY_TOOLS = frozenset({"suggest_products", "pair_upsell", "stage_phone_cart", "remember_caller"})

# One word of letters (any script) with ' or - inside: what budtender.identity.first_name accepts.
_NAME = re.compile(r"[^\W\d_](?:[^\W\d_]|['’-]){0,29}")
_WELCOME = re.compile(r"\s*Welcome to Happy Time[^!?]*!\s*", re.IGNORECASE)

_HEAD = (
    "CALLER (from our records; use silently to pick suggestions, never recite their history or "
    "numbers unless they ask):"
)
_ASK = (
    "If you have the remember_caller tool: early in the call, once, ask for their first name "
    "naturally and call remember_caller with it; never hold up their request for it; if they "
    "decline or were already asked, move on and do not ask again."
)
_NEW_CALLER = "CALLER is new to us and we do not know their name. " + _ASK


def dynamic_greeting() -> bool:
    """HHT_DYNAMIC_GREETING: the phone line answers assistant-request with a per-call squad."""
    return bool(getattr(settings, "HHT_DYNAMIC_GREETING", False))


# ── validation + sanitising ───────────────────────────────────────────────────


def clean_name(value: object) -> str:
    """The first word of ``value`` when it reads like a first name (letters, ' or - inside, 30 max),
    else "". Digits, braces and anything long are not a name. Capitalised like budtender does."""
    parts = str(value or "").split()
    tok = parts[0] if parts else ""
    if not _NAME.fullmatch(tok):
        return ""
    return tok.capitalize() if tok.isupper() or tok.islower() else tok


def _safe(value: object) -> str:
    """One inserted string, reduced to letters/digits/space and & ' - . (no braces, no newline), 30
    characters at most, and "" when it reads like an instruction. Vendor data lands here."""
    from voice.tools.faq import _looks_poisoned  # lazy: voice.tools imports this module's tool

    text = "".join(c for c in " ".join(str(value or "").split()) if c.isalnum() or c in " &'.-")
    text = " ".join(text.split())
    if _looks_poisoned(text):
        return ""
    return text[:30].strip()


def _list(value: object) -> list[str]:
    return [s for s in (str(v) for v in value if v) if s] if isinstance(value, list) else []


def _normalize(out: dict) -> dict:
    """Coerce a caller-context reply once, at the boundary (a present-but-null field is not absent)."""
    orders, days = out.get("orders"), out.get("days_since_last")
    return {
        "created": bool(out.get("created")),
        "known": bool(out.get("known")),
        "first_name": clean_name(out.get("first_name")),
        "has_history": bool(out.get("has_history")),
        "orders": orders if isinstance(orders, int) and not isinstance(orders, bool) else 0,
        "days_since_last": days if isinstance(days, int) and not isinstance(days, bool) else None,
        "top_categories": _list(out.get("top_categories")),
        "price_tier": str(out.get("price_tier") or ""),
        "brands": _list(out.get("brands")),
        "flavors": _list(out.get("flavors")),
        "terpenes": _list(out.get("terpenes")),
    }


# ── the per-call cache ─────────────────────────────────────────────────────────


def _key(call_id: str) -> str:
    return f"caller:{call_id}"


def cached(call_id: str) -> dict:
    """The cached context for a call, or ``{}`` (a cache outage reads as a miss)."""
    if not call_id:
        return {}
    try:
        hit = cache.get(_key(call_id))
    except Exception:  # noqa: BLE001 - a cache outage must not break a call
        logger.warning("caller cache unreadable", exc_info=True)
        return {}
    return hit if isinstance(hit, dict) else {}


def put(call_id: str, ctx: dict) -> None:
    if not call_id or not ctx:
        return
    try:
        cache.set(_key(call_id), ctx, TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.warning("caller cache unwritable", exc_info=True)


def for_call(
    call_id: str, number: str, store: str, *, fetch: bool = True, timeout: float = FETCH_TIMEOUT
) -> dict:
    """The public caller context for this call: the cached one, else (``fetch``) one budtender
    lookup that is then cached. ``{}`` when unknown. Never raises. A failed lookup is not cached,
    so the next request tries again."""
    hit = cached(call_id)
    if hit or not fetch:
        return hit
    from voice.recognition import normalize_e164

    e164 = normalize_e164(number)
    if not e164 or not capabilities.is_enabled("call.recognize_caller"):
        return {}
    try:
        from voice.budtender_client import budtender

        out = budtender().caller_context(
            e164, store=store, session_token=f"vc-{call_id}" if call_id else None, timeout=timeout
        )
    except Exception:  # noqa: BLE001 - any failure is "unknown", the call carries on
        logger.warning("caller context lookup failed", exc_info=True)
        return {}
    if not isinstance(out, dict) or not out.get("ok"):
        return {}
    ctx = _normalize(out)
    put(call_id, ctx)
    return ctx


# ── what the agents say and see ────────────────────────────────────────────────


def greeting(ctx: dict, base_first_message: str) -> str:
    """The entry greeting. A caller with a first name hears "Welcome back to Happy Time, <Name>!"
    in place of the base message's leading welcome (the rest of the base is kept); a base that does
    not open with the expected welcome gets it prefixed. No name, no base, or the owner's
    ``call.greet_by_name`` switch off: the base message, unchanged."""
    name = clean_name((ctx or {}).get("first_name"))
    if not base_first_message or not name or not capabilities.is_enabled("call.greet_by_name"):
        return base_first_message
    hello = f"Welcome back to Happy Time, {name}!"
    match = _WELCOME.match(base_first_message)
    rest = base_first_message[match.end() :] if match else base_first_message
    return f"{hello} {rest}".strip()


def context_text(ctx: dict) -> str:
    """The CALLER line every agent reads (the ``{{caller_context}}`` variable). Plain, never holds
    the phone number, built from sanitised fields. "" when the caller is unknown."""
    if not ctx:
        return ""
    on = capabilities.is_enabled("call.greet_by_name")
    name = clean_name(ctx.get("first_name")) if on else ""
    history = bool(ctx.get("has_history"))
    if not name and not history:
        return _NEW_CALLER if on else ""
    who = [name] if name else []
    if history:
        who.append("returning customer")
        days = ctx.get("days_since_last")
        if isinstance(days, int) and not isinstance(days, bool) and days >= 0:
            who.append("last bought today" if days == 0 else f"last bought {days} {'day' if days == 1 else 'days'} ago")
    parts = [", ".join(who) + "."]
    if history:
        for label, key in (("Usually buys", "top_categories"), ("Brands:", "brands"), ("Flavors:", "flavors")):
            items = [s for s in (_safe(v) for v in ctx.get(key) or []) if s]
            if items:
                parts.append(f"{label} {', '.join(items)}.")
        tier = _safe(ctx.get("price_tier"))
        if tier:
            parts.append(f"Typical price tier: {tier}.")
    if on and not name:
        parts.append("We do not know their first name. " + _ASK)
    return f"{_HEAD} {' '.join(parts)}"


def variable_values(ctx: dict) -> dict:
    """The per-call Vapi variables this module owns (the store variables stay in the webhook)."""
    on = capabilities.is_enabled("call.greet_by_name")
    return {
        "caller_context": context_text(ctx),
        "caller_first_name": clean_name((ctx or {}).get("first_name")) if on else "",
    }
