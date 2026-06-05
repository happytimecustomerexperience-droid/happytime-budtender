"""
Margin-first product ranking with customer-affinity, effect, category and
budget terms. Margin is normalized across the candidate set so it leads without
overriding a clearly on-profile pick.
"""
from __future__ import annotations

import re

from .models import CustomerProfile, Product

# Anonymous: margin-first. Logged-in: taste leads, margin still matters. The two
# weight sets are blended by whether we have a customer profile.
W_ANON = {"margin": 0.55, "affinity": 0.0, "effect": 0.18, "category": 0.05, "bucket": 0.12, "quality": 0.0, "budget": 0.10}
W_KNOWN = {"margin": 0.22, "affinity": 0.34, "effect": 0.10, "category": 0.04, "bucket": 0.12, "quality": 0.14, "budget": 0.04}

# Profit nudge by bucket (balanced — never overrides taste/budget gates).
BUCKET_NUDGE = {"profit": 1.0, "core": 0.4, "traffic": 0.0}
_TIER_CENTER = {"value": -0.6, "mid": 0.0, "top": 0.6}

# Minimum on-hand units to suggest. Owner policy: NEVER offer anything with fewer
# than 3 units on hand — avoids near-sold-out picks (qoh 1-2). (The real OOS problem
# was the stale-Supabase fallback + cross-category bleed, both fixed — not this floor.)
MIN_STOCK = 3

EFFECT_HINTS = {
    "relaxed": {"indica", "myrcene", "linalool", "kush"},
    "uplifted": {"sativa", "limonene", "pinene", "haze"},
    "middle": {"hybrid"},
}

CATEGORY_BY_SLOTKEY = {
    "flower": "flower",
    "concentrate": "concentrates",
    "cartridge": "vape-cartridges",
    "edible": "edibles",
    "tincture": "tinctures",
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


def _size_match(p: Product, size: str | None) -> bool:
    """Match a product to the requested size. Uses Dutchie's real unitWeight
    (grams) / effectivePotencyMg (dose) when present, falling back to a
    name-substring match. Returns True (no opinion) when size is open-ended."""
    if not size or size in ("any", "stock-up", "disposable"):
        return True
    # Pre-roll PACK sizes: 'single' (no pack marker) or 'Npk' (exact pack count).
    if size == "single":
        return parse_pack_count(p.name) is None
    if size.endswith("pk"):
        try:
            want = int(size[:-2])
        except ValueError:
            want = None
        if want is not None:
            return parse_pack_count(p.name) == want
    tgt = _parse_size_target(size)
    if tgt:
        unit, val = tgt
        if unit == "g":
            eg = _effective_grams(p)
            return eg is not None and abs(eg - val) <= 0.3
        if unit == "mg" and p.potency_mg:
            return float(p.potency_mg) >= 20 if val >= 20 else abs(float(p.potency_mg) - val) <= 2.5
    # No structured field → fall back to matching size tokens in the name.
    toks = _SIZE_SYNONYMS.get(size)
    if not toks:
        return True
    hay = (p.name or "").lower()
    return any(t in hay for t in toks)


def _effect_score(p: Product, desired: str | None) -> float:
    if not desired:
        return 0.0
    hints = EFFECT_HINTS.get(desired, set())
    hay = f"{p.strain} {p.strain_type} {p.dominant_terpene} {p.name}".lower()
    return 1.0 if any(h in hay for h in hints) else 0.0


def _affinity_score(p: Product, profile: CustomerProfile | None) -> float:
    if not profile:
        return 0.0
    s = 0.0
    # Brand + strain_type are the strongest "feels like mine" signals.
    s += 1.6 * float(profile.brand_affinity.get(p.brand, 0)) if p.brand else 0
    s += 1.0 * float(profile.strain_type_affinity.get(p.strain_type, 0)) if p.strain_type else 0
    s += 0.6 * float(profile.category_affinity.get(p.category, 0)) if p.category else 0
    s += 0.6 * float((profile.subcategory_affinity or {}).get(p.subcategory, 0)) if p.subcategory else 0
    if p.dominant_terpene:
        s += 0.4 * float(profile.terpene_affinity.get(p.dominant_terpene, 0))
    return min(s, 1.0)


def _quality_fit(p: Product, profile: CustomerProfile | None) -> float:
    """1.0 when the product sits at the customer's usual quality tier, fading
    with distance (uses the peer-relative price_z from classification)."""
    if not profile or not profile.price_tier:
        return 0.0
    center = _TIER_CENTER.get(profile.price_tier, 0.0)
    return 1.0 - min(abs(float(p.price_z or 0) - center) / 2.0, 1.0)


def _novelty_bias(p: Product, profile: CustomerProfile | None) -> float:
    """Habitual buyers (low novelty) get a boost for brands they already buy;
    explorers (high novelty) get a boost for brands they have NOT bought (same
    taste envelope, something new). Returns roughly -0.3..+0.3."""
    if not profile or not p.brand:
        return 0.0
    known = float(profile.brand_affinity.get(p.brand, 0)) > 0
    nov = float(profile.novelty_score or 0)
    if known:
        return 0.3 * (1.0 - nov)      # reward familiar for habitual
    return 0.3 * nov                  # reward new brands for explorers


def _recent_affinity(profile: CustomerProfile | None, top: int = 3) -> tuple[set, set]:
    """Brand/category from the customer's MOST RECENT purchases — so picks track
    what they're buying lately (RFM recency), not just lifetime favorites."""
    if not profile or not profile.purchase_history:
        return set(), set()
    items = [h for h in profile.purchase_history if h.get("last_bought_at")]
    items.sort(key=lambda h: str(h.get("last_bought_at")), reverse=True)
    recent = items[:top]
    return ({h.get("brand") for h in recent if h.get("brand")},
            {h.get("category") for h in recent if h.get("category")})


def _recency_boost(p: Product, recent_brands: set, recent_cats: set) -> float:
    """Small additive nudge toward the customer's most-recent brand/category."""
    b = 0.0
    if p.brand and p.brand in recent_brands:
        b += 0.10
    if p.category and p.category in recent_cats:
        b += 0.05
    return b


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


def rank_products(location: str, slots: dict, profile: CustomerProfile | None,
                  limit: int = 5, exclude_skus: set[str] | None = None) -> list[tuple[Product, str]]:
    exclude_skus = exclude_skus or set()
    cat_slot = slots.get("category")
    category = CATEGORY_BY_SLOTKEY.get(cat_slot, cat_slot) if cat_slot else None
    # Prefer an explicit dollar range; fall back to the tier bounds (chat route).
    if slots.get("price_min") is not None or slots.get("price_max") is not None:
        lo = float(slots.get("price_min") or 0)
        hi = float(slots.get("price_max") or 1e9)
    else:
        lo, hi = price_tier_bounds(slots.get("price_tier"))

    # "Premium" intent: top tier, or the open-ended high "$100 & up" bucket. For
    # premium intent, price is a PREFERENCE (show the priciest of the requested
    # weight), NOT a hard gate — otherwise a weight with nothing above the floor
    # would return the WRONG weight. Bounded ranges (e.g. $20–40) stay a hard
    # filter. WEIGHT always wins over price.
    premium_intent = (slots.get("price_tier") == "top") or (lo >= 100)

    qs = Product.objects.filter(location_slug=location, availability=True, quantity_on_hand__gte=MIN_STOCK)
    if category:
        qs = qs.filter(category=category)
    candidates = [p for p in qs if p.sku not in exclude_skus]
    # Granular subtype (rosin / gummies / lollipops…) — a HARD filter when chosen.
    sub = slots.get("subcategory")
    if sub:
        candidates = [p for p in candidates if product_subtype(p.name, p.category) == sub]
    if not premium_intent:
        candidates = [p for p in candidates if lo <= float(p.price) <= hi]
    if not candidates:
        return []

    # DOH filter (HARD): when the customer asks for DOH-certified only, suggest
    # ONLY products with "DOH" in the name. Explicit choice — no fallback to
    # non-DOH items (return nothing rather than something off-spec).
    if slots.get("doh_only"):
        candidates = [p for p in candidates if "doh" in (p.name or "").lower()]
        if not candidates:
            return []

    # ---- Size handling (HARD, with a capped NEAREST-weight fallback) ----
    # Respect the chosen weight. Show that weight; only if there aren't enough
    # exact matches do we fill UP TO 2 of `limit` slots with the NEAREST OTHER
    # weight — closest by grams (4g → 3.5g, NOT the 28g ounce), capped to a sane
    # window so we never substitute something wildly off (a 1g shake / a bulk oz).
    size = slots.get("size")
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
            nearby = rest
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
    # sensitive customers (value tier) make traffic-drivers acceptable.
    W = W_KNOWN if profile else W_ANON
    price_sensitive = bool(profile and profile.price_tier == "value")
    recent_brands, recent_cats = _recent_affinity(profile)

    scored = []
    for p in candidates:
        margin_norm = (float(p.margin) - m_lo) / span
        budget_fit = 1 - min(abs(float(p.price) - mid) / (mid or 1), 1)
        nudge = BUCKET_NUDGE.get(p.bucket, 0.4)
        if p.bucket == "traffic" and price_sensitive:
            nudge = 0.6  # cheap loss-leaders are fine for bargain hunters
        if profile and profile.bucket_mix:
            # Blend the business profit-nudge with the customer's OWN core/traffic/
            # profit mix, so picks match the KIND of products they actually buy.
            nudge = 0.6 * nudge + 0.4 * float(profile.bucket_mix.get(p.bucket, 0.0))
        score = (
            W["margin"] * margin_norm
            + W["affinity"] * _affinity_score(p, profile)
            + W["effect"] * _effect_score(p, desired)
            + W["category"] * (1.0 if category and p.category == category else 0.0)
            + W["bucket"] * nudge
            + W["quality"] * _quality_fit(p, profile)
            + W["budget"] * budget_fit
            + _novelty_bias(p, profile)
            + _recency_boost(p, recent_brands, recent_cats)
        )
        why = _why(p, desired, profile)
        scored.append((score, p, why))

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
        picks = list(ranked[:limit])
        chosen = {p.sku for _, p, _ in picks}
        # Fill any shortfall with up to 2 NEAREST-weight options (closest first).
        if len(picks) < limit and nearby:
            for p in nearby[: min(2, limit - len(picks))]:
                if p.sku not in chosen:
                    picks.append((0.0, p, _why(p, desired, profile)))
                    chosen.add(p.sku)
        return [(p, why) for _, p, why in picks[:limit]]

    scored.sort(key=lambda t: t[0], reverse=True)

    picks: list[tuple] = []
    used_skus: set[str] = set()

    # For a known customer, RESERVE up to 2 slots for brands they actually buy
    # (when in stock) so the set visibly feels like theirs — the rest fills by
    # price spread. This is the "familiar + adjacent-new + profit" educated mix.
    if profile and profile.brand_affinity:
        familiar = [(s, p, w) for s, p, w in scored
                    if p.brand and float(profile.brand_affinity.get(p.brand, 0)) > 0]
        seen_brands: set[str] = set()
        for s, p, w in familiar:
            if len(picks) >= 2:
                break
            if p.brand in seen_brands:   # one product per familiar brand, for variety
                continue
            seen_brands.add(p.brand)
            picks.append((s, p, w))
            used_skus.add(p.sku)

    # Spread the remaining slots across DISTINCT prices that exist in the range
    # (incl. odd ones like $65/$75/$85), highest-score item at each price point.
    remaining = [(s, p, w) for s, p, w in scored if p.sku not in used_skus]
    best_at_price: dict[float, tuple] = {}
    for s, p, w in remaining:  # scored desc → first seen at a price = best score
        pr = round(float(p.price), 2)
        if pr not in best_at_price:
            best_at_price[pr] = (s, p, w)
    prices_sorted = sorted(best_at_price)
    need = max(limit - len(picks), 0)
    if prices_sorted and need:
        if len(prices_sorted) <= need:
            spread = [best_at_price[pr] for pr in prices_sorted]
        else:
            n = len(prices_sorted)
            idxs = sorted({int(i * (n - 1) / (need - 1)) for i in range(need)}) if need > 1 else [0]
            j = 0
            while len(idxs) < need and j < n:
                if j not in idxs:
                    idxs.append(j)
                j += 1
            idxs = sorted(set(idxs))[:need]
            spread = [best_at_price[prices_sorted[i]] for i in idxs]
        for s, p, w in spread:
            if p.sku not in used_skus:
                picks.append((s, p, w))
                used_skus.add(p.sku)

    # Backfill if still short (sparse catalog / few distinct prices).
    for s, p, w in scored:
        if len(picks) >= limit:
            break
        if p.sku not in used_skus:
            picks.append((s, p, w))
            used_skus.add(p.sku)

    # Still short because the exact weight is sparse → fill UP TO 2 slots with the
    # NEAREST OTHER weight (closest grams first, e.g. 4g → 3.5g, never the ounce).
    if len(picks) < limit and nearby:
        cap = min(2, limit - len(picks))
        added = 0
        for p in nearby:   # pre-sorted: closest weight, best margin within it
            if added >= cap:
                break
            if p.sku in used_skus:
                continue
            picks.append((0.0, p, _why(p, desired, profile)))
            used_skus.add(p.sku)
            added += 1

    picks.sort(key=lambda t: t[0], reverse=True)
    return [(p, why) for _, p, why in picks[:limit]]


def _why(p: Product, desired: str | None, profile: CustomerProfile | None) -> str:
    """A short, PERSUASIVE reason to pick THIS one — built only from real product
    signals (never a receipt, never a fake claim). Ordered strongest-converting
    first: a personal hook, a live deal, the effect they asked for, real potency,
    and genuine scarcity. We surface the two strongest and always return a nudge."""
    bits: list[str] = []

    # 1. Personal hook — the strongest converter when we know their taste.
    if profile and p.brand and float(profile.brand_affinity.get(p.brand, 0)) >= 0.25:
        bits.append(f"your go-to {p.brand}")
    elif profile and p.strain_type and float(profile.strain_type_affinity.get(p.strain_type, 0)) >= 0.4:
        bits.append(f"right in your {p.strain_type.lower()} lane")
    elif profile and p.subcategory and float((profile.subcategory_affinity or {}).get(p.subcategory, 0)) >= 0.4:
        bits.append(f"your usual {p.subcategory}")
    elif profile and profile.price_tier and _quality_fit(p, profile) >= 0.7:
        bits.append("exactly your usual quality")

    # 2. Live deal — a real markdown is a powerful nudge.
    try:
        if p.price_was and float(p.price_was) - float(p.price) >= 1:
            save = float(p.price_was) - float(p.price)
            bits.append(f"on sale — save ${save:.0f}")
    except (TypeError, ValueError):
        pass

    # 3. The effect they actually asked for.
    if desired and _effect_score(p, desired):
        bits.append(f"dialed in for {desired}")

    # 4. Real potency (only when genuinely high).
    try:
        if p.thc_percent and float(p.thc_percent) >= 25:
            bits.append(f"hits hard at {float(p.thc_percent):.0f}% THC")
    except (TypeError, ValueError):
        pass

    # 5. Genuine scarcity — urgency, only when stock is truly low.
    try:
        if p.quantity_on_hand is not None and 0 < int(p.quantity_on_hand) <= 5:
            bits.append("almost gone")
    except (TypeError, ValueError):
        pass

    # 6. Flavor / strain fallback so there's always a reason to lean in.
    if len(bits) < 2 and p.dominant_terpene:
        bits.append(f"{p.dominant_terpene.lower()}-forward")
    elif len(bits) < 2 and p.strain:
        bits.append(p.strain)

    picked = [b for b in bits if b][:2]
    if not picked:
        return f"a standout {p.brand} pick" if p.brand else "a standout pick"
    s = " · ".join(picked)
    return s[0].upper() + s[1:]
