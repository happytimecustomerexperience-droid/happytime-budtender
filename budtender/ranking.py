"""
Margin-first product ranking with customer-affinity, effect, category and
budget terms. Margin is normalized across the candidate set so it leads without
overriding a clearly on-profile pick.
"""
from __future__ import annotations

import re

# The scoring brain lives in engine.py (shared verbatim with the in-store POS).
# ranking.py keeps the ORM query, slot/size/DOH filtering, the facet helpers and
# the owner ordering; it delegates the per-item demand score + persuasive reason
# to the engine so the formula can never drift between website and POS again.
from .engine import (AROMA_BOOST, MIN_STOCK, _recent_affinity, _request_weights,
                     from_product, profile_dict, score_one)
from .engine import why as _engine_why
from .engine import W_ANON, W_KNOWN  # noqa: F401 — re-exported for views._clean_ranking_weights
from .models import CustomerProfile, Product
from . import customer_model, lab_enrich, live_stock, product_attrs, terpenes

# Contract B: `sort_by` re-orders the already-filtered set. Anything else is ignored.
SORT_MODES = ("potency", "price_asc")
# Search v2: a shopper sees at most this many picks for one set of criteria (5 + "show 5 more" x3).
SEARCH_CAP = 20
# Requested terpenes nudge the demand score by up to this much (the strongest match gets all of it).
TERPENE_BOOST = 0.15


def _live_price(live, p: Product) -> float:
    """Sales-floor price when we have one, else the enrichment row's."""
    row = live.get(p.sku, p.product_id) if live.usable else None
    if row and row.get("price"):
        return float(row["price"])
    return float(p.price or 0)


CATEGORY_BY_SLOTKEY = {
    "flower": "flower",
    "concentrate": "concentrates",
    "cartridge": "vape-cartridges",
    "edible": "edibles",
    "tincture": "tinctures",
    # The canonical category is "pre-rolls" (dutchie.py), but the slot key was missing here, so
    # a pre-roll request had no way through even once the voice enum allowed it.
    "pre-roll": "pre-rolls",
    # 2026-08-10: live Dutchie inventory has 5 more in-stock categories than this map knew about
    # (97 products, unreachable by any caller). Added explicitly — even where the natural slot
    # key already equals the canonical category name (blunt, infused-blunt) — so the
    # TOOL_SPECS-enum ⇄ CATEGORY_BY_SLOTKEY mirror test (test_category_drift_alarm.py) can check
    # membership by key instead of relying on rank_products' `.get(key, key)` fallback.
    "topical": "topicals",
    "capsule": "capsules",
    "mint": "mints",
    "blunt": "blunt",
    "infused-blunt": "infused-blunt",
}


def price_tier_bounds(tier: str | None) -> tuple[float, float]:
    if tier == "value":
        return 0, 20
    if tier == "mid":
        return 20, 40
    if tier == "top":
        return 40, 1e9
    return 0, 1e9


def _round_nice(x: float) -> int:
    """Round a price boundary to a clean number — nearest $5 (nearest $10 above $100)."""
    step = 10 if x >= 100 else 5
    return max(step, int(round(x / step) * step))


def price_bands(prices: list[float]) -> list[dict]:
    """Data-driven price buckets for the SELECTED category+size, so the budget
    step is granular and relevant (a 1g cart and a 28g ounce get very different
    ranges). Returns quartile-based, nicely-rounded bands + an "Any price" escape.
    Falls back to a single "Any" band when there's too little to split."""
    prices = sorted(float(p) for p in prices if p is not None)
    any_band = {"value": "any", "label": "Any price", "hint": "Show all options"}
    if len(prices) < 4:
        return [any_band]
    lo, hi = prices[0], prices[-1]

    def q(frac: float) -> float:
        return prices[min(len(prices) - 1, max(0, int(round(frac * (len(prices) - 1)))))]

    # Quartile cut points, rounded to clean numbers, strictly increasing, inside (lo, hi).
    cuts: list[int] = []
    for frac in (0.25, 0.5, 0.75):
        c = _round_nice(q(frac))
        if c <= lo or c >= hi:
            continue
        if cuts and c <= cuts[-1]:
            continue
        cuts.append(c)
    if not cuts:
        return [any_band]

    bands: list[dict] = [{"value": f"u{cuts[0]}", "label": f"Under ${cuts[0]}", "min": 0, "max": cuts[0]}]
    for a, b in zip(cuts, cuts[1:]):
        bands.append({"value": f"{a}-{b}", "label": f"${a} – ${b}", "min": a, "max": b})
    bands.append({"value": f"{cuts[-1]}+", "label": f"${cuts[-1]} & up", "min": cuts[-1], "max": 1_000_000})
    bands.append(any_band)
    return bands


# Size tokens → substrings that may appear in a Dutchie product name. Used as a
# SOFT filter: we narrow to matching sizes, but if that leaves too few picks we
# fall back to ignoring size so the customer always gets options.
_SIZE_SYNONYMS = {
    "1g": ("1g", "1 g", "1gram", "1 gram", "(1g)"),
    "2g": ("2g", "2 g", "2gram", "2 gram"),
    "3.5g": ("3.5g", "3.5 g", "eighth", "1/8"),
    "7g": ("7g", "7 g", "quarter", "1/4"),
    "14g": ("14g", "14 g", "half oz", "half ounce", "1/2 oz"),
    "28g": ("28g", "28 g", "ounce", "1 oz", "1oz", "oz"),
    "0.5g": ("0.5g", ".5g", "half gram", "half-gram", "0.5 g"),
    "5mg": ("5mg", "5 mg"),
    "10mg": ("10mg", "10 mg"),
    "20mg+": ("20mg", "25mg", "50mg", "100mg"),
}

# Pre-roll PACK count parsed from the product name ("... 5pk" / "10 pack" /
# "6 joints" → 5 / 10 / 6). unit_weight is per-TOTAL grams for pre-rolls, so the
# pack count — the dimension customers actually shop by — only lives in the name.
_PACK_RE = re.compile(r"(\d+)\s*(?:pk|pack|joints?|ct|count)\b", re.I)


def parse_pack_count(name: str | None) -> int | None:
    """Pack count from a pre-roll name ('… 5pk' → 5). None when the name has no
    pack marker — i.e. a single pre-roll."""
    m = _PACK_RE.search(name or "")
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 1 <= n <= 100 else None


def _parse_size_target(size: str) -> tuple[str, float] | None:
    s = size.lower().strip()
    m = re.search(r"([\d.]+)", s)
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    if "mg" in s:
        return ("mg", val)
    if "g" in s:
        return ("g", val)
    return None


_GRAM_BUCKETS = (0.5, 1.0, 2.0, 3.5, 7.0, 14.0, 28.0)
# Real retail flower/concentrate weights (grams) that may appear in a product
# NAME. The labeled name is what the customer sees + pays for, so we trust an
# explicit weight token here over a mis-synced Dutchie unitWeight (e.g. a $120
# "…White Cherries 14g" flower that Dutchie tags unitWeight=3.5). The whitelist
# keeps strain numbers ("Gelato 33") and 'oz'/'lb' from false-matching.
_REAL_GRAMS = frozenset({0.5, 1.0, 2.0, 3.5, 4.0, 5.0, 7.0, 8.0, 10.0, 14.0, 28.0})
_NAME_GRAM_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*g\b", re.IGNORECASE)


def _name_grams(name: str | None) -> float | None:
    """Explicit real-weight token in the product name (e.g. '14g'), else None."""
    for m in _NAME_GRAM_RE.finditer(name or ""):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        if v in _REAL_GRAMS:
            return v
    return None


def _effective_grams(p: Product) -> float | None:
    """Reconciled gram weight: a real-weight token in the NAME wins over Dutchie's
    unitWeight (occasionally mis-synced), else unitWeight. Use this everywhere a
    product's weight is matched, so a mislabeled package can't slip the wrong
    size into results."""
    ng = _name_grams(p.name)
    if ng is not None:
        return ng
    return float(p.unit_weight) if p.unit_weight else None


def size_label(unit_weight: float | None, potency_mg: float | None, category: str | None) -> str:
    """Normalized subcategory label for peer grouping + size filtering.
    Grams for flower/concentrate/cart; mg dose for edibles/tinctures."""
    cat = (category or "").lower()
    if cat in ("edibles", "tinctures") and potency_mg:
        mg = float(potency_mg)
        if mg <= 7:
            return "5mg"
        if mg <= 14:
            return "10mg"
        return "20mg+"
    if unit_weight:
        w = float(unit_weight)
        best = min(_GRAM_BUCKETS, key=lambda g: abs(g - w))
        if abs(best - w) <= 0.3:
            return f"{best:g}g"
    return ""


_SIZE_WORDS = {
    "gram": "1g", "a gram": "1g", "half gram": "0.5g", "half-gram": "0.5g", "eighth": "3.5g", "an eighth": "3.5g",
    "1/8": "3.5g", "quarter": "7g", "a quarter": "7g", "1/4": "7g", "half ounce": "14g", "half oz": "14g",
    "1/2 oz": "14g", "ounce": "28g", "an ounce": "28g", "oz": "28g", "zip": "28g", "single": "single",
}
_OPEN_SIZES = ("any", "stock-up", "disposable")


def normalize_size(raw) -> str | None:
    """A size as a customer, a chip or a model wrote it → the canonical value the sizes endpoint emits
    ('5pk', 'single', '3.5g', '10mg', '20mg+'), or None when it is not a size we can read. Never a guess:
    '5 pack' / '5-pack' / '5 ct' → '5pk'; 'an eighth' → '3.5g'; 'large' → None."""
    if not isinstance(raw, str):
        return None
    s = " ".join(raw.strip().lower().split())
    if s in _OPEN_SIZES or s == "20mg+":
        return s
    if s in _SIZE_WORDS:
        return _SIZE_WORDS[s]
    m = re.fullmatch(r"(\d{1,3}) ?-? ?(?:pk|pks|pack|packs|ct|count|pc|pcs|piece|pieces)", s)
    if m:
        return f"{int(m.group(1))}pk" if 1 <= int(m.group(1)) <= 100 else None
    m = re.fullmatch(r"(\d+(?:\.\d+)?) ?(g|grams?|mg)", s)
    if m:
        return f"{float(m.group(1)):g}{'mg' if m.group(2) == 'mg' else 'g'}"
    return None


def _size_match(p: Product, size: str | None) -> bool:
    """Match a product to the requested size. Uses Dutchie's real unitWeight
    (grams) / effectivePotencyMg (dose) when present, falling back to a
    name-substring match. True (no opinion) only when size is open-ended; a size
    we cannot read matches NOTHING, never everything (a '5 pack' chip once
    returned 10-packs because an unread size meant "no filter")."""
    if not size or size in _OPEN_SIZES:
        return True
    size = normalize_size(size) or size
    # PACK sizes: 'single' (no pack marker) or 'Npk' (exact pack count).
    if size == "single":
        return parse_pack_count(p.name) is None
    m = re.fullmatch(r"(\d{1,3})pk", size)
    if m:
        return parse_pack_count(p.name) == int(m.group(1))
    tgt = _parse_size_target(size) if re.fullmatch(r"\d+(?:\.\d+)?(?:g|mg)\+?", size) else None
    if tgt:
        unit, val = tgt
        if unit == "g":
            eg = _effective_grams(p)
            return eg is not None and abs(eg - val) <= 0.3
        if p.potency_mg:
            return float(p.potency_mg) >= 20 if val >= 20 else abs(float(p.potency_mg) - val) <= 2.5
        # No stored dose → the dose written in the name, or no match.
        toks = _SIZE_SYNONYMS.get(size) or (f"{val:g}mg", f"{val:g} mg")
        hay = (p.name or "").lower()
        return any(t in hay for t in toks)
    return False


# ── Granular product subtypes (rosin, gummies, lollipops, …) ─────────────────
# Derived from the product NAME within a canonical category, most-specific first
# (first match wins). The questionnaire's subtype step is DATA-DRIVEN: the
# subtypes endpoint only returns the ones actually present in live inventory, so
# a brand-new form (e.g. a "lollipop" SKU we've never stocked) appears as a
# clickable option automatically the moment it's synced.
_SUBTYPE_KEYWORDS: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "concentrates": [
        # Rosin (folds in "live rosin") and Live Resin (folds in "cured resin") —
        # customers don't distinguish those near-duplicate pairs, so don't split them.
        ("rosin", ("rosin",)),
        ("live-resin", ("live resin", "cured resin")),
        ("rso", ("rso", "rick simpson", "feco")),
        ("distillate", ("distillate",)),
        ("diamonds", ("diamond",)),
        ("sauce", ("sauce",)),
        ("badder", ("badder", "batter", "budder")),
        ("shatter", ("shatter",)),
        ("crumble", ("crumble",)),
        ("sugar", ("sugar",)),
        ("wax", ("wax",)),
        ("hash", ("bubble hash", "hash", "temple ball")),
        ("kief", ("kief",)),
        ("applicator", ("applicator", "syringe")),
    ],
    "edibles": [
        ("gummies", ("gummy", "gummies")),
        ("peanut-butter-cups", ("peanut butter",)),
        ("chocolate", ("chocolate",)),
        ("lollipops", ("lollipop", "lolli", "sucker")),
        ("caramels", ("caramel",)),
        ("cookies", ("cookie",)),
        ("brownies", ("brownie",)),
        ("mints-tablets", ("mint", "lozenge", "tablet", "troche")),
        ("drinks", ("drink", "beverage", "soda", "seltzer", "shot", "syrup", "elixir", "tea")),
        ("capsules", ("capsule", "softgel", "pill")),
        ("hard-candy", ("hard candy", "candy")),
    ],
    "vape-cartridges": [
        # Oil TYPE only. "Disposable"/"Pod" are hardware FORMATS that overlap every
        # oil type (a Live Resin Disposable is both) — offering them here is the
        # duplication, so they're dropped. Rosin folds in "live rosin".
        ("rosin", ("live rosin", "rosin")),
        ("live-resin", ("live resin", "cured resin")),
        ("distillate", ("distillate",)),
    ],
    "pre-rolls": [
        # Pack count is the SIZE step now (Single / 5-pack / …) — don't duplicate it
        # here. Only the real product-type split: infused (vs not) and blunt.
        ("infused", ("infused", "diamond", "hash hole", "moon rock", "moonrock")),
        ("blunt", ("blunt",)),
    ],
    # Flower intentionally has NO subtype split: the only thing that qualified was
    # "Smalls", which makes a useless one-option "what type?" question — so the
    # questionnaire skips straight from Flower to the weight/size step.
}


def product_subtype(name: str | None, category: str | None) -> str:
    """Granular subtype for a product from its name within a category — e.g.
    'rosin', 'gummies', 'lollipops'. Returns '' when no recognizable form."""
    hay = (name or "").lower()
    for value, keys in _SUBTYPE_KEYWORDS.get((category or "").lower(), []):
        if any(k in hay for k in keys):
            return value
    return ""


_SUBTYPE_LABELS = {"rso": "RSO", "live-resin": "Live Resin", "live-rosin": "Live Rosin"}


def subtype_label(value: str) -> str:
    """Human label for a subtype value ('peanut-butter-cups' → 'Peanut Butter Cups')."""
    if value in _SUBTYPE_LABELS:
        return _SUBTYPE_LABELS[value]
    return value.replace("-", " ").title() if value else ""


# ── Available SIZES (data-driven questionnaire dimension) ────────────────────
# The size step is DATA-DRIVEN like subtypes: we derive the distinct sizes that
# ACTUALLY exist in live inventory for a (category[, subtype]) and expose them so
# the questionnaire renders real options — flower's 1/2/3.5/4/7/8/14/28g, a
# pre-roll's single/1pk…28pk — instead of a hardcoded guess. Two axes:
#   • gram  (flower / concentrates / vape-cartridges) — from Dutchie unitWeight
#   • pack  (pre-rolls / blunts)                       — parsed from the name
# A new weight/pack appears the moment a matching SKU syncs. Categories with no
# reliable size axis (edibles' potency_mg is package-total + wildly noisy) return
# [] → the questionnaire skips the size step gracefully.
_PACK_SIZE_CATEGORIES = ("pre-rolls", "blunt", "infused-blunt")
_GRAM_SIZE_CATEGORIES = ("flower", "concentrates", "vape-cartridges")
_GRAM_HINTS = {
    0.5: "Half gram", 1.0: "Gram", 2.0: "2 grams", 3.5: "Eighth", 4.0: "4 grams",
    7.0: "Quarter", 8.0: "8 grams", 10.0: "10 grams", 14.0: "Half oz", 28.0: "Ounce",
}


def size_dimension(category: str | None) -> str:
    """Which size axis a category shops by: 'pack', 'gram', or '' (none)."""
    cat = (category or "").lower()
    if cat in _PACK_SIZE_CATEGORIES:
        return "pack"
    if cat in _GRAM_SIZE_CATEGORIES:
        return "gram"
    return ""


def _size_for(name: str | None, unit_weight, category: str | None) -> str:
    """Canonical size VALUE on the category's axis: pre-rolls → 'single'|'5pk';
    gram cats → '3.5g' (snapping float noise to the nearest real weight, keeping
    rare weights like 4g/8g/10g literal); '' when indeterminate."""
    dim = size_dimension(category)
    if dim == "pack":
        n = parse_pack_count(name)
        return f"{n}pk" if n else "single"
    if dim == "gram":
        ng = _name_grams(name)
        if ng is not None:
            return f"{ng:g}g"          # labeled retail weight wins over unitWeight
        if unit_weight:
            w = float(unit_weight)
            best = min(_GRAM_BUCKETS, key=lambda g: abs(g - w))
            g = best if abs(best - w) <= 0.15 else round(w, 2)
            return f"{g:g}g"
    return ""


def size_value_label(value: str) -> tuple[str, str | None]:
    """Human (label, hint) for a size value. '5pk' → ('5-pack','5 pre-rolls');
    'single' → ('Single','1 pre-roll'); '3.5g' → ('3.5g','Eighth')."""
    if value == "single":
        return ("Single", "1 pre-roll")
    if value.endswith("pk"):
        n = value[:-2]
        return (f"{n}-pack", f"{n} pre-roll" if n == "1" else f"{n} pre-rolls")
    if value.endswith("g"):
        try:
            return (value, _GRAM_HINTS.get(float(value[:-1])))
        except ValueError:
            return (value, None)
    return (value, None)


def available_sizes(rows, category: str | None, min_count: int = 1) -> list[dict]:
    """Distinct in-stock sizes with counts for a category, sorted on its axis
    (single → packs asc → grams asc). `rows` is an iterable of (name, unit_weight).
    EVERY real in-stock size is shown (min_count=1) — even a single-product size like
    a lone 5g is a genuine option. (The 'can't suggest once DOH is applied' case is
    handled by SKIPPING the DOH step when DOH isn't viable, not by hiding the size.)
    [] when the category has no reliable size axis (e.g. edibles)."""
    counts: dict[str, int] = {}
    for name, unit_weight in rows:
        v = _size_for(name, unit_weight, category)
        if v:
            counts[v] = counts.get(v, 0) + 1

    def sort_key(v: str):
        if v == "single":
            return (0, 0.0)
        if v.endswith("pk"):
            return (1, float(v[:-2]))
        if v.endswith("g"):
            return (2, float(v[:-1]))
        return (3, 0.0)

    out: list[dict] = []
    for v in sorted(counts, key=sort_key):
        if counts[v] < min_count:
            continue
        label, hint = size_value_label(v)
        out.append({"value": v, "label": label, "hint": hint, "count": counts[v]})
    return out


# ── Search v2: every hard filter in ONE place ────────────────────────────────
# `eligible` is THE candidate set. rank_products draws from it, and every options/facet endpoint
# (facets.py) counts on it, so a button can never lead somewhere the search would not go. The stock
# gate is the first thing applied and nothing after it can add a product back.
_STRAIN_MATCH = {
    # Strain-type matching is HYPHEN-TOLERANT: live inventory carries "Indica-Hybrid" (41 SKUs)
    # and "Sativa-Hybrid" (25 SKUs) alongside the plain types, and an exact-match here dropped
    # both entirely for an "indica"/"sativa" ask. "indica"/"sativa" match their own hyphenated
    # variant only (never cross to the other side). DECISION: "hybrid" ALSO matches both
    # hyphenated variants — an Indica-Hybrid or Sativa-Hybrid genuinely IS a hybrid, and a caller
    # asking for "hybrid" wants the broadest hybrid shelf, not just the unhyphenated slice of it.
    "indica": ("indica", "indica-hybrid"),
    "sativa": ("sativa", "sativa-hybrid"),
    "hybrid": ("hybrid", "indica-hybrid", "sativa-hybrid"),
}
_INFUSION_ALIASES = {"diamonds": "diamond", "moon-rock": "moonrock", "moon-rocks": "moonrock",
                     "live-rosin-infused": "live-rosin", "thca": "diamond"}


def _num(v, lo: float = 0.0, hi: float = 100.0) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x != x:  # NaN
        return None
    return min(max(x, lo), hi)


def _flag(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    return isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on")


def _str_list(v, limit: int, item_len: int = 40) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return []
    out: list[str] = []
    for x in v:
        s = product_attrs.norm_text(x)[:item_len] if isinstance(x, str) else ""
        if s and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def _slug_value(v) -> str:
    """'Live Resin' / 'live_resin' / 'live-resin' -> 'live-resin'."""
    return product_attrs.norm_text(v).replace(" ", "-") if isinstance(v, str) else ""


def effective_size(slots: dict) -> str | None:
    """`size`, else the `pack` slot as a size value: 5 / "5" / "5pk" -> "5pk", "single" -> "single"."""
    size = slots.get("size")
    if isinstance(size, str) and size.strip():
        # Canonical when readable; an unreadable size is kept as sent so it matches nothing (fail
        # closed) — dropping it would search every size, the "5 pack → 10-packs" bug.
        return normalize_size(size) or size.strip()
    pack = slots.get("pack")
    if isinstance(pack, bool) or pack in (None, ""):
        return None
    if isinstance(pack, (int, float)):
        return f"{int(pack)}pk" if 1 <= int(pack) <= 100 else None
    if isinstance(pack, str):
        s = pack.strip().lower()
        if s == "single":
            return "single"
        m = re.fullmatch(r"(\d{1,3})\s*(?:pk|pack)?", s)
        if m and 1 <= int(m.group(1)) <= 100:
            return f"{int(m.group(1))}pk"
    return None


def parse_filters(slots: dict) -> dict:
    """The search-v2 slot keys, validated (anything malformed is simply ignored, never an error)."""
    slots = slots if isinstance(slots, dict) else {}
    terps = []
    for t in _str_list(slots.get("terpenes"), 5):
        c = terpenes.canonical(t)
        if c and c not in terps:
            terps.append(c)
    q = slots.get("q")
    q_tokens = [t for t in re.split(r"[^a-z0-9.%]+", product_attrs.norm_text(q[:80]))
                if len(t) >= 2][:8] if isinstance(q, str) else []
    infusion = []
    for t in _str_list(slots.get("infusion"), 5):
        v = _slug_value(t)
        v = _INFUSION_ALIASES.get(v, v)
        if v and v not in infusion:
            infusion.append(v)
    return {
        "thc_min": _num(slots.get("thc_min")),
        "thc_max": _num(slots.get("thc_max")),
        "cann": {name: v for key, name in product_attrs.CANNABINOID_SLOTS.items()
                 if (v := _num(slots.get(key))) is not None and v > 0},
        "terpenes": terps,
        "terpene_total_min": _num(slots.get("terpene_total_min")),
        "tags": _str_list(slots.get("tags"), 10),
        "q": q_tokens,
        "infusion": infusion,
        "solventless": _flag(slots.get("solventless")),
        "lab_tested": _flag(slots.get("lab_tested")),
    }


def resolve_category(cat_slot) -> tuple[tuple[str, ...] | None, str | None]:
    """(catalog slugs, master predicate | None) for a `category` slot. A master value ('vape-carts',
    'Disposable Vape', 'infused-pre-rolls'...) resolves to its catalog slugs plus the master to test; a
    legacy slot key / catalog slug resolves exactly as it always did (CATEGORY_BY_SLOTKEY, else itself)."""
    if not cat_slot or not isinstance(cat_slot, str):
        return None, None
    master = product_attrs.resolve_master(cat_slot)
    if master:
        value, _, cats = product_attrs.MASTERS[master]
        if cats == (value,):     # the master IS the whole catalog slug (flower, concentrates, topicals...)
            return cats, None
        return cats, master
    cat = CATEGORY_BY_SLOTKEY.get(cat_slot, cat_slot)
    return (cat,), None


def kinds_of(p: Product, info=None) -> set[str]:
    """Every subtype value a product answers to: its legacy first-match subtype (unchanged meaning:
    'rosin' covers live rosin, 'live-resin' covers cured resin) plus, for concentrates/vapes, every
    extraction/form its name or tags name (live-rosin, hash-rosin, cured-resin, sauce, diamonds...)."""
    kinds = product_attrs.extraction_kinds(p, info)
    legacy = product_subtype(p.name, p.category)
    if legacy:
        kinds.add(legacy)
    return kinds


def master_of(p: Product, info=None) -> str | None:
    return product_attrs.master_of(p, info, product_subtype(p.name, p.category))


def price_window(slots: dict) -> tuple[float, float, bool]:
    """(lo, hi, premium_intent). Prefer an explicit dollar range; fall back to the tier bounds (chat route).
    "Premium" intent: top tier, or the open-ended high "$100 & up" bucket. For premium intent, price is a
    PREFERENCE (show the priciest of the requested weight), NOT a hard gate — otherwise a weight with
    nothing above the floor would return the WRONG weight. Bounded ranges (e.g. $20–40) stay a hard
    filter. WEIGHT always wins over price."""
    if slots.get("price_min") is not None or slots.get("price_max") is not None:
        lo = _num(slots.get("price_min"), 0, 1e9) or 0.0
        hi = _num(slots.get("price_max"), 0, 1e9) or 1e9   # 0 / missing = open-ended, as it always was
    else:
        lo, hi = price_tier_bounds(slots.get("price_tier"))
    # "$100 & up" (no ceiling) is premium; "$100–$150" is a bounded range like any other and stays a hard
    # gate (it used to skip the gate and return $200 items, 2026-10-09 audit).
    return float(lo), float(hi), (slots.get("price_tier") == "top") or (lo >= 100 and hi >= 1e9)


# slot groups `eligible(ignore=...)` can leave out (a facet ignores the step it is asking about)
FILTER_GROUPS = ("category", "subcategory", "blocklist", "price", "doh", "size", "thc", "cannabinoids",
                 "terpenes", "terpene_total", "tags", "q", "infusion", "solventless", "lab_tested")


def eligible(location: str, slots: dict, *, exclude_skus=None, labs=None, details=None, live=None,
             ignore=frozenset(), exact_size: bool = False) -> list[Product]:
    """In-stock products matching EVERY hard filter in `slots`, in a stable order (by id).

    In stock = `availability` AND sales-floor stock >= MIN_STOCK (the live pull's quantity when it is
    usable, else the table's). Then: category (incl. master split), subcategory (strain type, or any
    subtype/extraction kind), category blocklist, price band (not for premium intent unless sort_by),
    DOH, and the v2 slots (THC range, cannabinoid minimums, terpenes (ANY), total terpenes, tags (ANY), q
    (ALL tokens), infusion (ANY), solventless, lab_tested). `exact_size` also applies size/pack exactly
    (the facets do; rank_products applies size itself, with its capped nearest-weight fill). `labs` /
    `details` are the request's memos (one bulk read each, only when a slot needs them)."""
    slots = slots if isinstance(slots, dict) else {}
    exclude = {str(s) for s in (exclude_skus or ())}
    labs = lab_enrich.Memo() if labs is None else labs
    details = lab_enrich.Memo() if details is None else details
    live = live_stock.stock_map(location) if live is None else live
    f = parse_filters(slots)

    qs = Product.objects.filter(location_slug=location, availability=True)
    if not live.usable:
        qs = qs.filter(quantity_on_hand__gte=MIN_STOCK)
    cats, master = (None, None) if "category" in ignore else resolve_category(slots.get("category"))
    if cats:
        qs = qs.filter(category__in=cats) if len(cats) > 1 else qs.filter(category=cats[0])
    cands = [p for p in qs.order_by("id") if p.sku not in exclude]
    if live.usable:
        # Never recommend something that isn't on the sales floor right now: the live pull wins over a
        # beat-refreshed table that still thinks a sellout is in stock.
        cands = [p for p in cands if live.buyable(sku=p.sku, product_id=p.product_id, min_stock=MIN_STOCK)]
    if not cands:
        return []

    sub = None if "subcategory" in ignore else slots.get("subcategory")
    sub = sub if isinstance(sub, str) and sub else None
    sub_key = sub if sub in _STRAIN_MATCH else _slug_value(sub) if sub else None
    need_info = bool(master or (sub_key and sub_key not in _STRAIN_MATCH)
                     or (f["tags"] and "tags" not in ignore) or (f["q"] and "q" not in ignore)
                     or (f["infusion"] and "infusion" not in ignore)
                     or (f["solventless"] and "solventless" not in ignore))
    if need_info:
        lab_enrich.details_for([p.product_id for p in cands], memo=details)

    def info(p):
        return details.get(p.product_id) or None

    if master:
        cands = [p for p in cands if master_of(p, info(p)) == master]
    # Granular subtype (rosin / gummies / lollipops…) — a HARD filter when chosen. voice/chat.py's
    # ``_SUBCATEGORY_RE`` sends indica/sativa/hybrid under this SAME "subcategory" slot key regardless of
    # category, but that's strain type, so it is matched against Product.strain_type.
    if sub_key in _STRAIN_MATCH:
        wanted = _STRAIN_MATCH[sub_key]
        cands = [p for p in cands if (p.strain_type or "").lower() in wanted]
    elif sub_key:
        cands = [p for p in cands if sub_key in kinds_of(p, info(p))]

    # Category blocklist (HARD): "not a concentrate" must exclude that category. Slot-key-style strings
    # are normalized through CATEGORY_BY_SLOTKEY; unknown/garbage entries are ignored.
    if "blocklist" not in ignore:
        blocklist = slots.get("category_blocklist") or []
        blocked = {CATEGORY_BY_SLOTKEY.get(b, b) for b in blocklist if isinstance(b, str) and b}
        blocked &= set(CATEGORY_BY_SLOTKEY.values())
        if blocked:
            cands = [p for p in cands if p.category not in blocked]

    # Premium intent makes price a preference (priciest first), not a gate. An explicit sort_by replaces
    # that ordering, so the customer's budget band is a HARD filter again, premium or not.
    if "price" not in ignore:
        lo, hi, premium = price_window(slots)
        sort_by = slots.get("sort_by") if slots.get("sort_by") in SORT_MODES else None
        if not premium or sort_by:
            cands = [p for p in cands if lo <= _live_price(live, p) <= hi]

    # DOH filter (HARD): ONLY products with "DOH" in the name. No fallback to non-DOH items.
    if slots.get("doh_only") and "doh" not in ignore:
        cands = [p for p in cands if "doh" in (p.name or "").lower()]

    if exact_size and "size" not in ignore:
        size = effective_size(slots)
        if size and size not in ("any", "stock-up", "disposable"):
            cands = [p for p in cands if _size_match(p, size)]

    if f["tags"] and "tags" not in ignore:
        cands = [p for p in cands if product_attrs.tag_match(p, info(p), f["tags"])]
    if f["q"] and "q" not in ignore:
        def _hay(p):
            i = info(p)
            return " ".join([
                product_attrs.norm_text(" ".join([p.name or "", p.brand or "", p.strain or "", p.strain_type or "",
                                                  p.category or "", master_of(p, i) or ""])),
                " ".join(product_attrs.norm_text(t) for t in product_attrs.info_tags(i)),
                " ".join(k.replace("-", " ") for k in kinds_of(p, i)),
            ])
        cands = [p for p in cands if all(t in h for h in [_hay(p)] for t in f["q"])]
    if f["infusion"] and "infusion" not in ignore:
        cands = [p for p in cands if product_attrs.infusion_kinds(p, info(p)) & set(f["infusion"])]
    if f["solventless"] and "solventless" not in ignore:
        cands = [p for p in cands if product_attrs.is_solventless(p, info(p))]

    want_lab = ((f["thc_min"] is not None or f["thc_max"] is not None) and "thc" not in ignore) \
        or (f["cann"] and "cannabinoids" not in ignore) or (f["terpenes"] and "terpenes" not in ignore) \
        or (f["terpene_total_min"] and "terpene_total" not in ignore) or (f["lab_tested"] and "lab_tested" not in ignore)
    if not (want_lab and cands):
        return cands
    lab_enrich.labs_for([p.batch_id for p in cands], memo=labs)

    def lab(p):
        return labs.get(p.batch_id) or None

    if "thc" not in ignore and (f["thc_min"] is not None or f["thc_max"] is not None):
        lo_t = f["thc_min"] if f["thc_min"] is not None else 0.0
        hi_t = f["thc_max"] if f["thc_max"] is not None else 100.0

        def _thc_ok(p):
            thc = lab_enrich.effective_thc(p.thc_percent, lab(p))
            return isinstance(thc, (int, float)) and lo_t <= float(thc) <= hi_t
        cands = [p for p in cands if _thc_ok(p)]
    if f["cann"] and "cannabinoids" not in ignore:
        cands = [p for p in cands
                 if all(product_attrs.cannabinoid_pct(lab(p), n) >= v for n, v in f["cann"].items())]
    if f["terpenes"] and "terpenes" not in ignore:
        want = set(f["terpenes"])
        cands = [p for p in cands if want & set(product_attrs.lab_terpenes(lab(p)))]
    if f["terpene_total_min"] and "terpene_total" not in ignore:
        cands = [p for p in cands if product_attrs.total_terpenes(lab(p)) >= f["terpene_total_min"]]
    if f["lab_tested"] and "lab_tested" not in ignore:
        cands = [p for p in cands if product_attrs.has_lab(lab(p))]
    return cands


def rank_products(location: str, slots: dict, profile: CustomerProfile | None,
                  limit: int = 5, exclude_skus: set[str] | None = None,
                  ranking_weights: dict | None = None,
                  labs: dict | None = None) -> list[tuple[Product, str]]:
    """Ranked (Product, why) picks. `labs` is the caller's per-request memo of batch labs
    ({batch_id: stored lab | None}); it is filled here — one bulk read for the picks — and the
    caller reuses it, so a request never reads labs per product.

    The order is prefix-stable: the first N picks for `limit=N` are the first N for any larger limit,
    which is what makes search-v2 paging (offset) show the same criteria without repeats."""
    slots = slots if isinstance(slots, dict) else {}
    exclude_skus = exclude_skus or set()
    labs = lab_enrich.Memo() if labs is None else labs
    sort_by = slots.get("sort_by") if slots.get("sort_by") in SORT_MODES else None
    cats, _master = resolve_category(slots.get("category"))
    category = cats[0] if cats else None
    lo, hi, premium_intent = price_window(slots)

    # Every hard filter (stock first; see eligible). Soft behaviour below (the nearest-weight fill) only
    # ever draws from this set, so it can never leak a product outside category/stock/DOH/price/v2 slots.
    live = live_stock.stock_map(location)
    candidates = eligible(location, slots, exclude_skus=exclude_skus, labs=labs, live=live)
    if not candidates:
        return []

    # ---- Size handling (HARD, with a capped NEAREST-weight fallback) ----
    # Respect the chosen weight. Show that weight; only if there aren't enough
    # exact matches do we fill UP TO 2 of `limit` slots with the NEAREST OTHER
    # weight — closest by grams (4g → 3.5g, NOT the 28g ounce), capped to a sane
    # window so we never substitute something wildly off (a 1g shake / a bulk oz).
    # A PACK size (single / 5pk) is an exact choice: no fill from other pack counts.
    size = effective_size(slots)
    nearby: list[Product] = []
    size_fallback = False  # True when no exact-weight match exists at all
    if size and size not in ("any", "stock-up", "disposable"):
        exact_skus = {p.sku for p in candidates if _size_match(p, size)}
        exact = [p for p in candidates if p.sku in exact_skus]
        rest = [p for p in candidates if p.sku not in exact_skus]
        tgt = _parse_size_target(size)
        if tgt and tgt[0] == "g":
            target_g = tgt[1]
            # NEAREST other gram weights — smaller OR larger — within [0.5x, 2x] of
            # the request, so a scarce weight borrows its closest neighbour (4g →
            # 3.5g/7g) but never a 1g shake or a 28g ounce. Closest weight first;
            # margin breaks ties within a weight.
            nearby = [p for p in rest
                      if (eg := _effective_grams(p)) is not None
                      and 0.5 * target_g <= eg <= 2.0 * target_g]
            nearby.sort(key=lambda p: (abs((_effective_grams(p) or 1e9) - target_g), -float(p.margin)))
        else:
            # A pack count or a dose is an exact choice: "5-pack" never fills with 10-packs or singles,
            # "10mg" never with 100mg. Only a gram weight borrows its nearest neighbour (above).
            nearby = []
        if exact:
            candidates = exact
        elif nearby:
            # No exact match (e.g. "1g flower" isn't a real eighth) → fall back to
            # the CLOSEST weight. Mark it so premium ordering prefers closest-weight.
            candidates = nearby
            nearby = []
            size_fallback = True
        else:
            # A specific size was requested but NOTHING of that size or a near
            # neighbour survives the category/subtype/price/DOH filters. Be HONEST:
            # return no picks rather than substituting an unrelated weight — the
            # chat then shows "no matches for these filters".
            candidates = []

    if not candidates:
        return []

    margins = [float(p.margin) for p in candidates]
    m_lo, m_hi = min(margins), max(margins)
    span = (m_hi - m_lo) or 1.0
    desired = slots.get("effect_desired")
    # Premium intent rewards the top of the range; otherwise center on the mid.
    mid = min(hi, 1_000_000) if premium_intent else (lo + min(hi, 200)) / 2

    # Taste leads when we know the customer; margin-first when anonymous. Price-
    # sensitive customers (value tier) make traffic-drivers acceptable. The score
    # is the shared engine's — computed on a plain feature dict so the website and
    # the in-store POS rank on the exact same formula (see engine.score_one).
    W = _request_weights(ranking_weights, profile)
    price_sensitive = bool(profile and profile.price_tier == "value")
    pf = profile_dict(profile)
    recent_brands, recent_cats = _recent_affinity(pf)
    ctx = {
        "W": W, "m_lo": m_lo, "span": span, "mid": mid, "desired": desired,
        "category": category, "price_sensitive": price_sensitive,
        "recent_brands": recent_brands, "recent_cats": recent_cats,
    }

    # The aroma slot (one of terpenes.AROMA_TERPENES; anything else is ignored) nudges the score of
    # products whose real batch lab carries it. DB-only: one bulk read for every candidate, memoized in
    # `labs` (the picks' labs below and the serializer reuse it); no slot, no read, no change.
    aroma = slots.get("aroma")
    aroma_hits: dict[str, tuple] = {}
    if isinstance(aroma, str) and aroma in terpenes.AROMA_TERPENES:
        lab_enrich.labs_for([p.batch_id for p in candidates], memo=labs)
        for p in candidates:
            hit = terpenes.aroma_hit((labs.get(p.batch_id) or {}).get("terpenes"), aroma)
            if hit:
                aroma_hits[p.sku] = (aroma, *hit)

    # (score, product, aroma hit | None): the reason is written once, for the final picks only (_finish).
    # Requested terpenes (search v2 `terpenes`): `eligible` already kept only products whose lab carries
    # one of them; here the AMOUNT ranks — the strongest combined share earns the full TERPENE_BOOST.
    want_terps = parse_filters(slots)["terpenes"]
    terp_amount: dict[str, float] = {}
    if want_terps:
        lab_enrich.labs_for([p.batch_id for p in candidates], memo=labs)
        for p in candidates:
            have = product_attrs.lab_terpenes(labs.get(p.batch_id))
            terp_amount[p.sku] = sum(have.get(t, 0.0) for t in want_terps)
    terp_top = max(terp_amount.values(), default=0.0) or 1.0

    # Tailoring (customer_model): a med/high-confidence customer's own buying (ratio, form, extraction,
    # per-piece mg, their category's price/THC band, their last buy) nudges the score. SOFT only: these
    # candidates already passed every hard filter, and None (anonymous / new / low confidence) leaves the
    # score exactly as it was. One bulk read of the candidates' labs + details when it applies.
    tailor = customer_model.tailor_for(profile)
    fits: dict[str, dict] = {}
    tboost: dict[str, float] = {}
    if tailor is not None:
        details = lab_enrich.Memo()
        lab_enrich.labs_for([p.batch_id for p in candidates], memo=labs)
        lab_enrich.details_for([p.product_id for p in candidates], memo=details)
        for p in candidates:
            fits[p.sku] = tailor.match(p, details.get(p.product_id) or None, labs.get(p.batch_id) or None,
                                       price=_live_price(live, p))

    scored = []
    for p in candidates:
        score = score_one(from_product(p), pf, ctx)
        if p.sku in aroma_hits:
            score += AROMA_BOOST
        if terp_amount:
            score += TERPENE_BOOST * terp_amount.get(p.sku, 0.0) / terp_top
        if p.sku in fits:
            tboost[p.sku] = tailor.boost(fits[p.sku])
            score += tboost[p.sku]
        scored.append((score, p, aroma_hits.get(p.sku)))

    # ---- Explicit sort (Contract B): "stronger" / "cheaper" re-orders THIS filtered set. ----
    # Every filter above (category, subtype, size, price band, DOH, exclusions) has already
    # applied; this only decides the order. It wins over the premium and margin-first orderings.
    if sort_by:
        if sort_by == "potency":
            # The displayed THC is the inventory number, else the batch lab's: sort on the same one.
            lab_enrich.labs_for([t[1].batch_id for t in scored if t[1].thc_percent is None], memo=labs)

            def _key(t):
                thc = lab_enrich.effective_thc(t[1].thc_percent, labs.get(t[1].batch_id))
                return (thc is None, -(thc or 0.0), -t[0], t[1].sku)   # highest first, unknowns last
        else:
            def _key(t):
                return (_live_price(live, t[1]), -t[0], t[1].sku)      # cheapest first
        picks = sorted(scored, key=_key)[:limit]
        if len(picks) < limit and nearby:
            have = {t[1].sku for t in picks}
            picks += [(0.0, p, None) for p in nearby if p.sku not in have][: limit - len(picks)]
        return _finish(picks, desired, profile, labs, tailor, fits)

    # ---- Premium intent: highest price of this category+weight wins. ----
    # The customer asked for the top end (top tier / "$100 & up"), so we order
    # strictly by price descending (score breaks ties) instead of the usual
    # mid-range spread. Up to 2 larger-weight items fill any shortfall.
    if premium_intent:
        if size_fallback:
            # No exact weight: honor the price preference WITHIN the closest
            # weight (smallest of the larger options first), so "1g flower"
            # surfaces premium 3.5g — never the 28g ounce.
            ranked = sorted(scored, key=lambda t: (float(t[1].unit_weight or 1e9), -float(t[1].price)))
        else:
            ranked = sorted(scored, key=lambda t: (float(t[1].price), t[0]), reverse=True)
        # Price-ordered, but cap a single brand at 2 of the set for variety
        # (only when other brands are available to take the slot instead).
        picks = []
        chosen: set[str] = set()
        bc: dict[str, int] = {}
        for t in ranked:
            if len(picks) >= limit:
                break
            b = (t[1].brand or "").strip().lower()
            if (b and bc.get(b, 0) >= 2
                    and any((x[1].brand or "").strip().lower() != b and x[1].sku not in chosen
                            for x in ranked)):
                continue
            picks.append(t)
            chosen.add(t[1].sku)
            if b:
                bc[b] = bc.get(b, 0) + 1
        # Fill any shortfall with up to 2 NEAREST-weight options (closest first).
        if len(picks) < limit and nearby:
            for p in nearby[: min(2, limit - len(picks))]:
                if p.sku not in chosen:
                    picks.append((0.0, p, None))
                    chosen.add(p.sku)
        return _finish(picks[:limit], desired, profile, labs, tailor, fits)

    scored.sort(key=lambda t: t[0], reverse=True)   # demand score, desc

    # ---- Owner ordering: #1 margin · #2 velocity · rest = real demand ----
    # Position 1 is the highest-MARGIN item, position 2 the highest-VELOCITY
    # (units/day) item, and positions 3+ the products customers actually want
    # (the affinity/effect/budget demand score). A SOFT brand-variety penalty
    # keeps the set from being five of the same label. Margin + velocity are
    # server-only — the serializer allowlist guarantees neither reaches the browser.
    picks: list[tuple] = []
    used_skus: set[str] = set()
    brand_count: dict[str, int] = {}

    def _take(t: tuple) -> None:
        picks.append(t)
        used_skus.add(t[1].sku)
        b = (t[1].brand or "").strip().lower()
        if b:
            brand_count[b] = brand_count.get(b, 0) + 1

    def _variety(t: tuple) -> float:
        # ×0.6 per prior use of this brand → a different brand wins ties, but a
        # clearly stronger same-brand item can still earn its slot.
        b = (t[1].brand or "").strip().lower()
        return 0.6 ** brand_count.get(b, 0) if b else 1.0

    def _varied(t: tuple) -> float:
        # Brand variety damps the demand score only, and not at all for a strong tailoring fit (the 2:1
        # gummy a ratio buyer takes, from the one brand that makes it). No tailoring -> score x variety,
        # exactly as before.
        tb = tboost.get(t[1].sku, 0.0)
        if tb >= customer_model.FIT_NO_VARIETY:
            return t[0]
        return (t[0] - tb) * _variety(t) + tb

    # #1 — highest gross-margin $ in the matching set.
    if profile:
        # ponytail: known shoppers use the existing blended score; add explicit
        # familiar/new quotas only if score+variety underperforms in conversion data.
        while len(picks) < limit:
            rest = [t for t in scored if t[1].sku not in used_skus]
            if not rest:
                break
            _take(max(rest, key=_varied))

        if len(picks) < limit and nearby:
            for p in nearby:
                if len(picks) >= limit:
                    break
                if p.sku in used_skus:
                    continue
                _take((0.0, p, None))

        return _finish(picks[:limit], desired, profile, labs, tailor, fits)

    _take(max(scored, key=lambda t: float(t[1].margin)))
    # #2 — highest sales velocity among the rest. With no transactions yet all
    # velocity is 0, so this tie-breaks to the top demand score (slot never wasted).
    rest = [t for t in scored if t[1].sku not in used_skus]
    if rest:
        _take(max(rest, key=lambda t: (float(t[1].velocity), t[0])))
    # #3..limit — real demand (the score), greedily, with soft brand variety.
    while len(picks) < limit:
        rest = [t for t in scored if t[1].sku not in used_skus]
        if not rest:
            break
        _take(max(rest, key=lambda t: t[0] * _variety(t)))

    # Sparse exact-weight catalog → fill any empty slots with the NEAREST other
    # weight (closest grams first, e.g. 4g → 3.5g, never the ounce).
    if len(picks) < limit and nearby:
        for p in nearby:
            if len(picks) >= limit:
                break
            if p.sku in used_skus:
                continue
            _take((0.0, p, None))

    # Order is intentional (#1 margin, #2 velocity, …) — do NOT re-sort by score.
    return _finish(picks[:limit], desired, profile, labs, tailor, fits)


def _why(p: Product, desired: str | None, profile: CustomerProfile | None, aroma_hit: tuple | None = None,
         tailored: str | None = None) -> str:
    """Persuasive reason for THIS pick — delegated to the shared engine so the
    website and the in-store POS speak the same language. The lab's words live in
    `lab.profile` (the card shows them once, from there), never in this reason; the one
    exception is the aroma the customer asked for, when the batch lab really carries it."""
    return _engine_why(from_product(p), desired, profile_dict(profile), aroma_hit, tailored=tailored)


def _finish(picks: list[tuple], desired: str | None, profile: CustomerProfile | None,
            labs: dict, tailor=None, fits: dict | None = None) -> list[tuple[Product, str]]:
    """The final picks with their reasons. The one bulk lab read for the picks happens
    here (memoized in `labs`, which the caller reuses for serialization), and reasons are
    written for the picks only — never for every candidate."""
    products = [t[1] for t in picks]
    lab_enrich.labs_for([p.batch_id for p in products], memo=labs)
    fits = fits or {}
    return [(t[1], _why(t[1], desired, profile, t[2],
                        tailor.why_bit(t[1], fits[t[1].sku]) if tailor is not None and t[1].sku in fits else None))
            for t in picks]
