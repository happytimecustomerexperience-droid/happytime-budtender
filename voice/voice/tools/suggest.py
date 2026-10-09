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
  * Lab facts → budtender's ``lab`` / ``info`` / ``size`` are never copied through; ``_facts`` builds
    fresh speakable fields from them (``thc_spoken``, ``terpenes_spoken``, ``allergens`` …) and a
    null lab yields no lab field at all. A figure the agent can read is a figure the tool returned.
  * Price gate (2026-10-06) → a price is per SIZE. A ``suggest_products`` / ``check_inventory`` call for
    a size-required category (``constants.SIZE_REQUIRED_CATEGORIES``; blank/unknown fails closed) with
    NO ``size`` slot returns NO price of any kind — the pick is built without ``price_otd`` /
    ``price_spoken`` — plus ``needs_size`` / ``size_options`` and a ``spoken_summary`` that asks the
    size. A ``price_max`` ceiling is not a size. The phone agent and the text brain both pass through
    here, so the guarantee is code, not prompt.
  * Margin-vs-taste switch = presence of a recognized caller (ADR-005). The handler passes the
    resolved phone/session to budtender; budtender owns the re-ranking — the voice repo never sorts.
  * ONE gated upsell (ADR-007) → ``pair_upsell`` voices a complement ONLY when
    ``strength >= PAIR_STRENGTH_GATE``; a silent (no-offer) response is correct, not a bug.
"""

from __future__ import annotations

import datetime
import logging
import math
import re
from decimal import Decimal

from voice import constants as C
from voice import pricing, recognition
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
# PUBLIC_PRODUCT_FIELDS. Nothing outside this list (plus ``_safe_links`` below) ever reaches the agent.
_SPEAKABLE_FIELDS = ("rank", "name", "brand", "strain", "thc_percent", "why_this", "sku")

# budtender's public_product also emits the product's lab report link and its exact online-menu slug
# (budtender a7f85c9). They were dropped here, so "can I see the COA on that" had nothing to answer
# from. Kept only when well-formed: the COA must be an https URL (any other scheme — http, data:,
# javascript: — is dropped), the slug a plain token the menu link can carry.
_HTTPS_URL_RE = re.compile(r"https://[^\s/?#<>\"'`]+[^\s<>\"'`]*")
_SLUG_RE = re.compile(r"[A-Za-z0-9][\w.~-]*")


def _safe_links(result: dict) -> dict:
    lab = result.get("lab") if isinstance(result.get("lab"), dict) else {}
    coa = str(result.get("coa_url") or lab.get("coa_url") or "").strip()
    slug = str(result.get("menu_slug") or "").strip()
    links = {}
    if _HTTPS_URL_RE.fullmatch(coa):
        links["coa_url"] = coa
    if _SLUG_RE.fullmatch(slug):
        links["menu_slug"] = slug
    return links


# ── lab + product facts the agent may SAY (2026-10-05) ─────────────────────────────
# budtender's public_product now carries ``lab`` (terpenes with %, THC/CBD, screens, COA, a hedged
# ``profile``), ``info`` (allowlisted product-record facts) and ``size``. Neither dict is copied
# through: ``_facts`` builds a handful of fresh, validated, speakable fields from them — the same
# pattern as ``price_spoken`` — so the only figures the agent can read are tool values, a lab that
# is null yields NO lab field (never a zero-fill, never a fallback to the old ``dominant_terpene``),
# and nothing outside this allowlist (cost/margin/vendor included, however deeply nested) can ride.
_PLAIN_TERPENE = {  # only the unambiguous four: alpha-/beta-pinene must never merge into "pinene"
    "beta-myrcene": "myrcene", "beta-caryophyllene": "caryophyllene",
    "alpha-humulene": "humulene", "alpha-bisabolol": "bisabolol",
}
_LAB_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ,'()+\-]{0,38}")
_SCREENS = (  # budtender's contaminant key -> what the agent says it passed
    ("pesticides", "pesticides"), ("heavy_metals", "heavy metals"), ("mycotoxin", "mycotoxins"),
    ("microbiology", "microbials"), ("solvents", "residual solvents"),
)
_ALLERGEN_CAP = 1000  # budtender's own cap: an allergen list is spoken whole or not at all
_PROFILE_LINE_CAP = 300
_EXPLAIN_CAP = 300  # budtender's lab.profile.explain is <= 280 chars; a longer one is not its sentence
_MAX_EXCLUDED = 100
_SORT_MODES = ("potency", "price_asc")  # budtender's Contract B whitelist
# The pick fields that carry a figure the agent may SAY (next to ``price_spoken``): the one list a
# Numbers-Guard check traces a spoken number back through. Tests import it, so a new spoken field
# cannot be added here without the guard seeing it.
SPOKEN_FACT_KEYS = (
    "size", "thc_spoken", "cbd_spoken", "total_terpenes_spoken", "terpenes_spoken",
    "minor_cannabinoids_spoken", "profile_line", "profile_explain", "tested_date",
)


def percent(value) -> float | None:
    """A real percentage — finite, > 0, ≤ 100 — else None. A bool is not a number and a string is
    not a figure: a malformed lab value speaks nothing rather than a repaired guess."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 < value <= 100 else None


def pct_text(value: float) -> str:
    """The tool's own figure as text — 27.3 -> '27.3', 2.0 -> '2', 0.93 -> '0.93'. Never rounded."""
    text = format(Decimal(repr(float(value))), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _lab_name(value) -> str | None:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if _LAB_NAME_RE.fullmatch(text) else None


def _ranked(items, *, terpene: bool, top: int) -> list[tuple[str, float]]:
    """[{"name","pct"}] -> up to ``top`` (name, pct) pairs, biggest first, dropping any entry without
    a usable name AND percentage. Terpene names are lower-cased and the common ones made plain."""
    rows = []
    for item in items if isinstance(items, list) else []:
        name = _lab_name(item.get("name")) if isinstance(item, dict) else None
        pct = percent(item.get("pct")) if isinstance(item, dict) else None
        if name and pct is not None:
            rows.append((_PLAIN_TERPENE.get(name.lower(), name.lower()) if terpene else name, pct))
    rows.sort(key=lambda row: -row[1])  # stable: the lab's own order breaks ties
    return rows[:top]


def _said(rows: list[tuple[str, float]]) -> str:
    return ", ".join(f"{name} at {pct_text(pct)} percent" for name, pct in rows)


def _facts(result: dict) -> dict:
    """The speakable size / potency / lab / allergen fields for one budtender row, built in code.
    Every key is present ONLY when its source holds a real value."""
    out: dict = {}
    size = result.get("size")
    if isinstance(size, str) and size.strip():
        out["size"] = size.strip()
    lab = result.get("lab") if isinstance(result.get("lab"), dict) else {}

    # ONE potency number per fact: budtender's ``thc_percent`` (it already folds in the lab's total
    # when the inventory has none). ``lab.thc_total`` is never read here — a second source.
    thc = percent(result.get("thc_percent"))
    if thc:
        out["thc_spoken"] = f"{pct_text(thc)} percent THC"
    cbd = percent(lab.get("cbd_total"))
    if cbd:
        out["cbd_spoken"] = f"{pct_text(cbd)} percent CBD"
    total = percent(lab.get("total_terpenes"))
    if total:
        out["total_terpenes_spoken"] = f"{pct_text(total)} percent total terpenes"
    terps = _ranked(lab.get("terpenes"), terpene=True, top=5)
    if terps:
        out["terpenes"] = [{"name": name, "pct": pct} for name, pct in terps]  # a "which has more" answer
        out["terpenes_spoken"] = _said(terps[:3])  # never recite more than three aloud
    minors = _ranked(lab.get("minor_cannabinoids"), terpene=False, top=3)
    if minors:
        out["minor_cannabinoids_spoken"] = _said(minors)
    profile = lab.get("profile") if isinstance(lab.get("profile"), dict) else {}
    line = profile.get("line")
    if isinstance(line, str) and line.strip() and len(line) <= _PROFILE_LINE_CAP:
        out["profile_line"] = line.strip()  # budtender's hedged, compliant line — verbatim
    # What the pick should smell like / the experience people describe: budtender's terpenes.profile
    # sentence (fixed phrases + the lab's own terpene names, already hedged, "everyone is different").
    # Verbatim or absent — the agent never writes its own, and `lab`/`info` are still never copied.
    explain = profile.get("explain")
    if isinstance(explain, str) and explain.strip() and len(explain) <= _EXPLAIN_CAP:
        out["profile_explain"] = explain.strip()
    screens = lab.get("contaminants") if isinstance(lab.get("contaminants"), dict) else {}
    passed = [label for key, label in _SCREENS if screens.get(key) == "pass"]
    if passed:
        out["lab_screens_passed"] = passed
    tested = lab.get("tested_date")
    if isinstance(tested, str):
        try:
            out["tested_date"] = datetime.date.fromisoformat(tested).isoformat()
        except ValueError:
            pass
    info = result.get("info") if isinstance(result.get("info"), dict) else {}
    allergens = info.get("allergens")
    if isinstance(allergens, str) and allergens.strip() and len(allergens) <= _ALLERGEN_CAP:
        out["allergens"] = allergens  # verbatim, whole — never inferred, never cut
    return out


_HONEST_EMPTY = "I'm not finding that in stock right now."


def _dedupe_results(raw_results: list[dict], limit: int = 3) -> list[dict]:
    deduped: list[dict] = []
    seen = set()
    seen_strains = set()
    should_dedupe = len(raw_results) > 3 and limit >= 3
    seen_brands = set()
    for r in raw_results:
        sku = str(r.get("sku") or "").strip()
        strain = str(r.get("strain") or "").strip().lower()
        brand = str(r.get("brand") or "").strip().lower()
        key = (sku, strain, brand)
        if key in seen:
            continue
        if should_dedupe and strain and strain in seen_strains:
            continue
        if should_dedupe and brand and brand in seen_brands:
            continue
        if not sku:
            continue
        seen.add(key)
        if strain:
            seen_strains.add(strain)
        if brand:
            seen_brands.add(brand)
        deduped.append(r)
        if len(deduped) >= limit:
            break
    return deduped


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


# ── the price gate ──────────────────────────────────────────────────────────────
# A price is per SIZE: an eighth and an ounce of the same flower cost very differently, so "how much is
# flower" has no single answer and a bare number would be a wrong one. ``needs_size`` is the ONE
# predicate; both handlers below build their result without any price when it holds.
SIZE_ASK = "Prices depend on the size"  # opens every size question — chat.py spots it on the agent's last line
SIZE_REASK = "I want to give you the right price"  # the same question, reworded
_SIZE_SPOKEN = {  # the everyday names; any other size is read from its own digits, never invented
    "0.5g": "a half gram", "1g": "a gram", "3.5g": "an eighth", "7g": "a quarter",
    "14g": "a half ounce", "28g": "an ounce",
}
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)(g|mg)")


def needs_size(args: dict) -> bool:
    """True when this call may not carry a price: its category needs a size and none was given. A
    blank or unknown category is not exempt (fail closed); ``price_max`` is not a size; neither are
    budtender's own no-opinion values ("any", "stock-up", "disposable")."""
    if _normalize_category(args.get("category")) in C.SIZE_EXEMPT_CATEGORIES:
        return False
    return str(args.get("size") or "").strip().lower() in C.NO_SIZE_VALUES


def _size_phrase(size: str) -> str:
    if size in _SIZE_SPOKEN:
        return _SIZE_SPOKEN[size]
    match = _SIZE_RE.fullmatch(size)
    return f"{match[1]} {'grams' if match[2] == 'g' else 'milligrams'}" if match else size


def _size_options(rows: list[dict]) -> list[str]:
    """The distinct shelf sizes budtender's rows actually carry, smallest first — never a size the
    shelf does not have. (Edibles and tinctures carry none: budtender states no per-unit size for them.)"""
    sizes = {r["size"].strip() for r in rows if isinstance(r.get("size"), str) and r["size"].strip()}

    def order(size: str):
        match = _SIZE_RE.match(size)
        return (float(match[1]) if match else math.inf, size)

    return sorted(sizes, key=order)


def _or_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else (
        f"{names[0]} or {names[1]}" if len(names) == 2 else f"{', '.join(names[:-1])}, or {names[-1]}"
    )


def size_question(options: list[str], *, again: bool = False) -> str:
    """The question that stands in for a price: built from ``options`` only. With none to offer it
    asks the open question rather than listing a size nobody confirmed. ``again`` is the same ask in
    other words (the caller pressed for a price without answering) — text chat sends it once, then
    hands the caller to a team member."""
    names = [_size_phrase(s) for s in options]
    if again:
        return f"{SIZE_REASK} — which size should I look up{': ' + _or_list(names) if names else ''}?"
    if not names:
        return f"{SIZE_ASK} — what size are you thinking?"
    return f"{SIZE_ASK} — are you thinking {_or_list(names)}?"


def is_size_question(line: str) -> bool:
    """Whether an agent line is one of our own size questions (either wording)."""
    return SIZE_ASK in (line or "") or SIZE_REASK in (line or "")


def _slots_from_args(args: dict, store: str) -> dict:
    """Fold the Vapi tool args into the budtender ``slots`` dict (11-P1 §4.1 mapping). Only
    explicitly-provided slots are forwarded (budtender treats each as a HARD filter). The
    ``category`` passes through ``_normalize_category`` so a router-classified cartridge stays
    ``cartridge`` (never silently rewritten to ``concentrate`` — the export-#4 fix)."""
    slots: dict = {"store": store}
    cat = args.get("category")
    if cat not in (None, ""):
        slots["category"] = _normalize_category(cat)
    for key in ("subcategory", "brand", "size", "price_tier", "effect_desired", "aroma"):
        val = args.get(key)
        if val not in (None, ""):
            slots[key] = val
    for key in ("price_min", "price_max"):
        val = args.get(key)
        if isinstance(val, (int, float)):
            slots[key] = val
    if isinstance(args.get("doh_only"), bool):
        slots["doh_only"] = args["doh_only"]
    # "stronger" / "cheaper": budtender re-orders the already-filtered set (Contract B). Only its
    # two known modes are forwarded; it ignores anything else, and so do we.
    if args.get("sort_by") in _SORT_MODES:
        slots["sort_by"] = args["sort_by"]
    return slots


def _clean_skus(value) -> list[str] | None:
    """The SKUs to leave out of a search — a bounded list of plain strings, else None."""
    if not isinstance(value, list):
        return None
    skus = [str(s).strip() for s in value
            if isinstance(s, (str, int)) and not isinstance(s, bool) and str(s).strip()]
    return skus[:_MAX_EXCLUDED] or None


def _speakable_pick(result: dict, store: str, *, priced: bool = True) -> dict:
    """Map a budtender result to the leak-safe spoken shape (11-P1 §4.5).

    Copies ONLY the ``_SPEAKABLE_FIELDS`` allowlist + relabels the (OTD-uplifted) ``price`` →
    ``price_otd`` (ADR-009) + adds the code-built lab/size/potency fields of ``_facts``. Drops
    everything else (image_url/dutchie_link/stock_on_hand/price_was — irrelevant on a voice channel)
    AND, defensively, anything outside the allowlist even though budtender already serialized
    leak-safe — the raw ``lab``/``info`` dicts included. The raw pre-tax ``price`` is NEVER copied.
    ``priced=False`` (the price gate) builds the pick WITHOUT ``price_otd`` / ``price_spoken``: the
    price is never computed, so no later step can speak it."""
    pick = {k: result.get(k) for k in _SPEAKABLE_FIELDS}
    pick.update(_safe_links(result))
    pick.update(_facts(result))
    if priced:
        pick["price_otd"] = pricing.otd(result.get("price"), store)
        pick["price_spoken"] = pricing.spoken(pick["price_otd"])  # voice reads THIS, never the digits
    else:
        pick["why_this"] = _why_without_dollars(pick.get("why_this"))
    return pick


def _why_without_dollars(why):
    """budtender's ``why_this`` can carry a dollar figure ("On sale — save $5 · …", engine.why). That is
    a price-derived number too, so a pick built without a price drops those segments (they are
    ``" · "``-joined) and keeps the rest of the reason."""
    if not isinstance(why, str) or "$" not in why:
        return why
    kept = " · ".join(part.strip() for part in why.split("·") if "$" not in part and part.strip())
    return kept[:1].upper() + kept[1:]


def _spoken_summary(picks: list[dict], lead: str = "My top pick is") -> str:
    """A short spoken lead-in built from the top pick's real fields (Numbers-Guard — every value is
    a budtender field, not invented). Empty picks → the honest-miss line. ``lead`` lets a follow-up
    ("stronger", "cheaper") open with its own acknowledgement instead of a fresh introduction."""
    if not picks:
        return _HONEST_EMPTY
    top = picks[0]
    name = top.get("name") or "this one"
    brand = top.get("brand")
    price = top.get("price_otd")
    # Don't double the brand when `name` already leads with it (case/punctuation-insensitive) —
    # e.g. name="Cannaquench Sparkling 5mg", brand="Cannaquench" must not read "Cannaquench
    # Cannaquench Sparkling 5mg".
    if brand:
        norm_name = re.sub(r"[^a-z0-9]+", "", name.lower())
        norm_brand = re.sub(r"[^a-z0-9]+", "", brand.lower())
        brand_repeated = bool(norm_brand) and norm_name.startswith(norm_brand)
    else:
        brand_repeated = False
    if brand and not brand_repeated:
        line = f"{lead} the {brand} {name}"
    else:
        line = f"{lead} the {name}"
    why = (top.get("why_this") or "").strip()
    if why:
        line += f" — {why}"
    # Potency and the top terpenes, exactly as ``_facts`` built them; absent → not mentioned.
    if top.get("thc_spoken"):
        line += f", {top['thc_spoken']}"
    if top.get("terpenes_spoken"):
        line += f", with {top['terpenes_spoken']}"
    spoken_price = pricing.spoken(price)
    if spoken_price:
        line += f", and it's {spoken_price} out the door."
    else:
        line += "."
    return line


spoken_summary = _spoken_summary  # public: chat.py words its own follow-up lead-ins with it


def _maybe_resolve_recognition(args: dict, ctx: dict) -> None:
    """Resolve the returning caller LAZILY on first use (memoized via ``ctx['recognition_resolved']``
    — 11-P1 §3.4). The raw caller number arrives on ``ctx['caller_number']`` (set by the webhook
    when available) OR is absent (blocked/anonymous → margin-first). No-op if already resolved."""
    if ctx.get("recognition_resolved"):
        return
    number = ctx.get("caller_number") or ""
    resolved = recognition.resolve_caller(number, ctx)
    if isinstance(resolved, dict):
        ctx.update(resolved)


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
    # A search needs SOMETHING to narrow on, but it does not have to be a category: "you guys
    # still carrying Phat Panda" (a brand) and "anything under twenty bucks" (a ceiling) are real
    # shopping asks with no category word in them, and hard-requiring one made them unanswerable —
    # the caller got an honest miss for a question the shelf could have answered. A call with
    # nothing to narrow on at all is still the tool error it always was. An EFFECT is narrowing
    # for the same reason a brand is: "can you recommend something good for just tonight" names
    # what the caller wants the product to do, which is exactly what budtender ranks on.
    narrowed = (
        (args.get("category") or "").strip()
        or (args.get("brand") or "").strip()
        or (args.get("effect_desired") or "").strip()
        or isinstance(args.get("price_max"), (int, float))
    )
    if not narrowed:
        return {"error": "missing_category", "picks": [], "spoken_summary": _HONEST_EMPTY}

    store = _resolve_store(args, ctx)
    _maybe_resolve_recognition(args, ctx)

    slots = _slots_from_args(args, store)
    exclude = _clean_skus(args.get("exclude_skus"))
    client = budtender()
    out = client.search(
        slots,
        limit=12,
        phone=ctx.get("_caller_phone"),  # presence → W_KNOWN; absence → W_ANON (margin-first)
        session_token=ctx.get("session_token"),
        exclude_skus=exclude,
        location=store,
        source="phone",
        record=False,  # 12 fetched, 3 spoken: only the spoken ones are reported below
    )
    results = out.get("results") or []
    # The price gate: no size on a size-required category -> the picks carry NO price (never computed)
    # and the result asks the size instead. Nothing found stays the honest miss, not a size question.
    gated = needs_size(args)
    # Fetch a bit wider than the final limit so dedupe can still return 3 useful options.
    # ponytail: one-wide fetch window; adjust the limit here if upstream quality drops.
    spoken = _dedupe_results(results, limit=6)[:3]
    picks = [_speakable_pick(r, store, priced=not gated) for r in spoken]
    _stamp_suggested(ctx, [p["sku"] for p in picks if p.get("sku")])
    if spoken:
        client.suggestions_shown(store, spoken, phone=ctx.get("_caller_phone"),
                                 session_token=ctx.get("session_token"))

    if gated and picks:
        options = _size_options(results)  # every real size the search found, not just the three shown
        return {
            "picks": picks, "needs_size": True, "size_options": options,
            "spoken_summary": size_question(options),
        }
    return {"picks": picks, "spoken_summary": _spoken_summary(picks)}


@register("check_inventory")
def handle_check_inventory(args: dict, ctx: dict) -> dict:
    """Purchasability for one SKU (never cost/margin), plus its OTD price ONLY under the price gate:
    the call must carry the ``size`` the caller chose (or the ``category`` of a product with no size
    concept), else the price is withheld and the result is ``needs_size`` (a SKU taken from an unsized
    search must not become a way around the gate). Returns ``{in_stock, qty_band, price_otd, …}``; an out-of-stock/zombie SKU →
    ``in_stock:false``."""
    args = args or {}
    ctx = ctx or {}
    sku = (args.get("sku") or "").strip()
    if not sku:
        return {"error": "missing_sku", "in_stock": False}
    store = _resolve_store(args, ctx)
    out = budtender().check_sku(store, sku)
    if not out.get("in_stock"):
        return {"in_stock": False}
    result = {
        "in_stock": True,
        "qty_band": _qty_band(out.get("stock_on_hand")),
        "name": out.get("name"),
        "thc_percent": out.get("thc_percent"),
        **_safe_links(out),
        **_facts(out),  # the same speakable size / THC / lab / allergen fields a pick carries
    }
    if needs_size(args):
        options = _size_options([out])  # the SKU's own shelf size, when budtender states one
        return {**result, "needs_size": True, "size_options": options,
                "spoken_summary": size_question(options)}
    result["price_otd"] = out.get("price_otd")
    result["price_spoken"] = pricing.spoken(out.get("price_otd"))  # voice reads THIS, never the digits
    return result


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
