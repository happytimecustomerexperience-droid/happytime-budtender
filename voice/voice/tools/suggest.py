"""The three Dutchie suggestion tool handlers (11-P1 §3.2) — registered into P0's ``TOOL_REGISTRY``.

``suggest_products`` / ``check_inventory`` / ``pair_upsell``: parse + validate the Vapi tool-call
args, resolve the caller's recognition handle (lazily, on first ``suggest_products`` use — 11-P1
§3.4 parallel-safety), call ``voice/budtender_client``, and shape the LEAK-SAFE, OTD, speakable
result the assistant reads. Each handler returns budtender values only — it never composes a figure
(Numbers-Guard, ADR-012); the central ``dispatch`` scrub (``guardrails.scrub_leak``) is a second
wall behind budtender's allowlist serializer (ADR-008).

House invariants (binding):
  * Leak-safe → ``_speakable_pick`` copies ONLY the §4.5 allowlist; ``price``→``price_otd`` relabel
    makes the OTD invariant explicit (ADR-009). Cost/margin physically never reach here.
  * Margin-vs-taste switch = presence of a recognized caller (ADR-005). The handler passes the
    resolved phone/session to budtender; budtender owns the re-ranking — the voice repo never sorts.
  * ONE gated upsell (ADR-007) → ``pair_upsell`` voices a complement ONLY when
    ``strength >= PAIR_STRENGTH_GATE``; a silent (no-offer) response is correct, not a bug.
"""

from __future__ import annotations

import logging

from voice import pricing
from voice.budtender_client import budtender
from voice.tools import register

logger = logging.getLogger(__name__)

# The upsell speak-or-stay-silent threshold (ADR-007; research §8.3 recommends ~0.4). A single
# tunable module constant (a P4 dashboard knob later — 21-SPEC §13).
PAIR_STRENGTH_GATE = 0.40

# The valid store slugs (budtender models.STORES; 21-SPEC §4.6). Default yakima.
_VALID_STORES = {"yakima", "mount-vernon", "pullman"}
_DEFAULT_STORE = "yakima"

# Cartridge category guard (P5 #4): once the router classifies an opener as a cartridge (a 510 / vape
# pen / disposable), the tool must forward ``category:"cartridge"`` UNCHANGED — a cartridge must
# NEVER be silently rewritten to ``concentrate`` (the export-#4 bug). The router's cartridge lexicon
# values all canonicalize to budtender's ``cartridge`` enum value here.
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

# The leak-safe → speakable allowlist (11-P1 §4.5) — a SUBSET of budtender's
# PUBLIC_PRODUCT_FIELDS. Nothing outside this list ever reaches the agent.
_SPEAKABLE_FIELDS = ("rank", "name", "brand", "strain", "thc_percent", "why_this", "sku")

_HONEST_EMPTY = "I'm not finding that in stock right now."


# ── helpers ─────────────────────────────────────────────────────────────────────
def _resolve_store(args: dict, ctx: dict) -> str:
    """Store slug from the tool arg, else the call's resolved store (ctx), else yakima."""
    store = (args.get("store") or ctx.get("store") or _DEFAULT_STORE).strip().lower()
    return store if store in _VALID_STORES else _DEFAULT_STORE


def _normalize_category(value) -> str:
    """Canonicalize a category arg to budtender's enum. The guard (P5 #4): any cartridge alias
    (cart / 510 / vape pen / disposable / AIO / pod) maps to ``cartridge`` — a cartridge is NEVER
    rewritten to ``concentrate``. A non-cartridge value passes through lower-cased + trimmed."""
    raw = str(value or "").strip().lower()
    if raw in _CARTRIDGE_ALIASES:
        return "cartridge"
    return raw


def _slots_from_args(args: dict, store: str) -> dict:
    """Fold the Vapi tool args into the budtender ``slots`` dict (11-P1 §4.1 mapping). Only
    explicitly-provided slots are forwarded (budtender treats each as a HARD filter). The
    ``category`` passes through ``_normalize_category`` so a router-classified cartridge stays
    ``cartridge`` (never silently rewritten to ``concentrate`` — the export-#4 fix)."""
    slots: dict = {"store": store}
    cat = args.get("category")
    if cat not in (None, ""):
        slots["category"] = _normalize_category(cat)
    for key in ("subcategory", "size", "price_tier", "effect_desired"):
        val = args.get(key)
        if val not in (None, ""):
            slots[key] = val
    for key in ("price_min", "price_max"):
        val = args.get(key)
        if isinstance(val, (int, float)):
            slots[key] = val
    if isinstance(args.get("doh_only"), bool):
        slots["doh_only"] = args["doh_only"]
    return slots


def _speakable_pick(result: dict, store: str) -> dict:
    """Map a budtender result to the leak-safe spoken shape (11-P1 §4.5).

    Copies ONLY the ``_SPEAKABLE_FIELDS`` allowlist + relabels the (OTD-uplifted) ``price`` →
    ``price_otd`` (ADR-009). Drops everything else (image_url/dutchie_link/stock_on_hand/price_was —
    irrelevant on a voice channel) AND, defensively, anything outside the allowlist even though
    budtender already serialized leak-safe. The raw pre-tax ``price`` is NEVER copied through."""
    pick = {k: result.get(k) for k in _SPEAKABLE_FIELDS}
    pick["price_otd"] = pricing.otd(result.get("price"), store)
    return pick


def _spoken_summary(picks: list[dict]) -> str:
    """A short spoken lead-in built from the top pick's real fields (Numbers-Guard — every value is
    a budtender field, not invented). Empty picks → the honest-miss line."""
    if not picks:
        return _HONEST_EMPTY
    top = picks[0]
    name = top.get("name") or "this one"
    brand = top.get("brand")
    price = top.get("price_otd")
    lead = f"My top pick is the {brand} {name}" if brand else f"My top pick is the {name}"
    why = (top.get("why_this") or "").strip()
    if why:
        lead += f" — {why}"
    if price:
        lead += f", and it's {price:.0f} out the door."
    else:
        lead += "."
    return lead


def _maybe_resolve_recognition(args: dict, ctx: dict) -> None:
    """Resolve the returning caller LAZILY on first use (memoized via ``ctx['recognition_resolved']``
    — 11-P1 §3.4). The raw caller number arrives on ``ctx['caller_number']`` (set by the webhook
    when available) OR is absent (blocked/anonymous → margin-first). No-op if already resolved."""
    if ctx.get("recognition_resolved"):
        return
    from voice import recognition

    number = ctx.get("caller_number") or ""
    recognition.resolve_caller(number, ctx)


def _stamp_suggested(ctx: dict, skus: list[str]) -> None:
    """Append the suggested SKUs onto the in-flight ``VoiceCall`` (outcome=suggested) — the durable
    record P4's call log reads (D4). Best-effort; never raises into the turn. The raw caller number
    is NEVER persisted — only the peppered hash (PII discipline)."""
    call_id = ctx.get("call_id")
    if not call_id or not skus:
        return
    try:
        from voice.models import Outcome, VoiceCall

        vc, _ = VoiceCall.objects.get_or_create(
            call_id=call_id,
            defaults={
                "store": ctx.get("store", ""),
                "caller_phone_hash": ctx.get("caller_phone_hash", ""),
            },
        )
        existing = list(vc.suggested_skus or [])
        merged = existing + [s for s in skus if s not in existing]
        vc.suggested_skus = merged
        vc.outcome = Outcome.SUGGESTED
        if ctx.get("caller_phone_hash") and not vc.caller_phone_hash:
            vc.caller_phone_hash = ctx["caller_phone_hash"]
        vc.save(update_fields=["suggested_skus", "outcome", "caller_phone_hash", "updated_at"])
    except Exception:  # noqa: BLE001 — stamping must never crash the suggestion turn
        logger.warning("failed to stamp suggested SKUs for %s", call_id, exc_info=True)


# ── handlers ────────────────────────────────────────────────────────────────────
@register("suggest_products")
def handle_suggest_products(args: dict, ctx: dict) -> dict:
    """Recommend ≤3 in-stock, leak-safe picks each with a speakable ``why_this`` + OTD price.

    Validates the required ``category`` (``store`` defaults to yakima), resolves recognition lazily
    (KNOWN → ``W_KNOWN`` taste-first / UNKNOWN → ``W_ANON`` margin-first), calls budtender, maps each
    result to the speakable shape, stamps the SKUs onto the ``VoiceCall``. Honest-empty when
    budtender returns no results (never fabricate — Numbers-Guard)."""
    args = args or {}
    ctx = ctx or {}
    if not (args.get("category") or "").strip():
        return {"error": "missing_category", "picks": [], "spoken_summary": _HONEST_EMPTY}

    store = _resolve_store(args, ctx)
    _maybe_resolve_recognition(args, ctx)

    slots = _slots_from_args(args, store)
    exclude = args.get("exclude_skus") if isinstance(args.get("exclude_skus"), list) else None
    out = budtender().search(
        slots,
        limit=3,
        phone=ctx.get("_caller_phone"),  # presence → W_KNOWN; absence → W_ANON (margin-first)
        session_token=ctx.get("session_token"),
        exclude_skus=exclude,
        location=store,
    )
    results = out.get("results") or []
    picks = [_speakable_pick(r, store) for r in results[:3]]
    _stamp_suggested(ctx, [p["sku"] for p in picks if p.get("sku")])

    return {"picks": picks, "spoken_summary": _spoken_summary(picks)}


@register("check_inventory")
def handle_check_inventory(args: dict, ctx: dict) -> dict:
    """Purchasability + OTD price for one SKU (never cost/margin). Returns
    ``{in_stock, qty_band, price_otd}``; an out-of-stock/zombie SKU → ``in_stock:false``."""
    args = args or {}
    ctx = ctx or {}
    sku = (args.get("sku") or "").strip()
    if not sku:
        return {"error": "missing_sku", "in_stock": False}
    store = _resolve_store(args, ctx)
    out = budtender().check_sku(store, sku)
    if not out.get("in_stock"):
        return {"in_stock": False}
    return {
        "in_stock": True,
        "qty_band": _qty_band(out.get("stock_on_hand")),
        "price_otd": out.get("price_otd"),
        "name": out.get("name"),
    }


@register("pair_upsell")
def handle_pair_upsell(args: dict, ctx: dict) -> dict:
    """ONE complementary add-on by anchor SKU, surfaced only when the strength gate clears
    (ADR-007). ``offer:true`` ⇒ speak the pair; ``offer:false`` ⇒ the agent stays silent."""
    args = args or {}
    ctx = ctx or {}
    anchor = (args.get("anchor_sku") or "").strip()
    if not anchor:
        return {"error": "missing_anchor_sku", "offer": False}
    store = _resolve_store(args, ctx)
    out = budtender().pair_for_sku(
        store,
        anchor,
        phone=ctx.get("_caller_phone"),
        session_token=ctx.get("session_token"),
    )
    pairing = out.get("pairing")
    strength = float(out.get("strength") or 0.0)
    if not pairing or strength < PAIR_STRENGTH_GATE:
        return {"offer": False}
    pair = _speakable_pick(pairing, store)
    return {
        "offer": True,
        "pair": pair,
        "reason_text": out.get("reason_text", ""),
        "strength": strength,
    }


def _qty_band(stock_on_hand) -> str:
    """Coarse stock band (never an exact count the agent doesn't need — 11-P1 §8 open question)."""
    try:
        n = int(stock_on_hand)
    except (TypeError, ValueError):
        return "available"
    if n <= 5:
        return "a few left"
    if n <= 20:
        return "in stock"
    return "plenty"


def register_all() -> None:
    """No-op explicit hook (handlers self-register via ``@register`` at import). Kept so the P0
    loader contract — ``from . import suggest`` triggers registration — is documented."""
